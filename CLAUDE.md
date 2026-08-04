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

**Deployed to the Pi and live (July 26, 2026).** The whole `rework` is
running on the Raspberry Pi 5: a gunicorn systemd service (`pifinance`)
auto-starting on boot, real bank data synced (income included — a real
paycheck landed as income, not spend), a daily SimpleFIN sync timer
(`pifinance-sync.timer`, 06:30), and an off-Pi "golden" backup. `v1.0`
is tagged at the pristine baseline `41c2040` on origin.

Deploy deviated deliberately from `deploy/pi-deploy.md`'s "v1.0-first
then gated migration" plan: a fresh Pi has no data to protect, so we
deployed the current app (`rework` HEAD) directly via `migrate.py init
finance.db --live` (full v5 schema), created accounts, connected the
bank. `deploy.sh`'s gated-migration path is for FUTURE updates now that
`finance.db` holds real data.

**Repo topology — reconciled (July 26, 2026):** `main` == `rework`'s tree.
`origin/main` was `e8f27d6`, a stale schema-v3 "hardening" state one merge
commit off the shared base (its hardening content already lived in
`rework` via the July 19 merge). Fixed by a merge commit (`09c8694`) whose
first parent is the old `origin/main` (so the push fast-forwarded — no
history rewrite) and whose tree is byte-identical to `rework`; `main` now
reflects what's deployed. Future increments still land on `rework`, gate,
then deploy from `main` via `deploy.sh main` (fast-forward `main` to
`rework` per increment). Deploy facts: Pi user `altamash`, path `/home/altamash/pifinance`;
systemd units carry a `pi`/`/home/pi` assumption — rewrite with `sed` on
copy, never edit the tracked file (future `git pull` would conflict).
**Tailscale — done (Jul 26, 2026).** Phone access is live: Mac, iPhone,
and the Pi are all on the tailnet under `altamashmomin@`. Pi tailnet IP
`100.108.237.13`, MagicDNS name `raspberrypi`; the app is reachable at
`http://raspberrypi:8080` (or the 100.x IP) over a direct P2P link (~32ms,
not relayed). Verified end-to-end from this Mac (which is on the tailnet):
`/api/status` → 200, `{"logged_in":false,"setup_required":false}`. No open
deploy/infra tasks remain.

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
sync both.

Income build underway (CORE-DESIGN sequence step 6, per INCOME-DESIGN):
- Migration #005 — income classification foundation (July 22, 2026):
  `transactions` gains `direction` (`'out'`|`'in'`, default `'out'`) and
  `income_type` (NULL until classification lands); `income_rules` table,
  unwired. Schema only — no verb touches these columns yet, so every
  existing row keeps its exact meaning. `set_paid_by` references
  `members(id)` per CORE-DESIGN's amendment. Two product decisions
  settled going into this build: **refund netting stays honest** (a
  refund reduces its category's spend total in the month it lands,
  accepted dip and all) and **auto-rule suggestion waits for a repeat
  match** rather than firing on the first tag — robustness over fast
  convergence, both per Alta's call. Gated with the enumerated diff
  (one empty table, schema_version bump; notes/005-gate-expectation);
  suite at 72 tests.

Out-of-sequence cleanup, found during a cohesion check right after
(July 22, 2026): bill definitions (`bills` table — name/amount/due
day/category, distinct from `mark_bill_paid`'s transaction-producing
verb) were the one mutating table still taking raw SQL from a route,
and their create/edit/delete wrote no audit row at all.
- `create_bill`/`update_bill`/`delete_bill` extracted: registry rows
  added first (growth rule), then the three verbs; bill routes are thin
  callers. Deployed validation preserved exactly, including an
  asymmetry kept rather than "fixed" — `create_bill` distinguishes
  "amount must be positive" from "due day must be between 1 and 31,"
  `update_bill` uses one combined message for both. Delete stays soft
  (`active=0`); past `bill_payments` and their transactions are
  untouched, matching `delete_goal`'s bounded-transition posture. True
  zero-diff gate dbe7119→2879986 (no schema change); suite at 90 tests.

Classification foundation built (July 22, 2026), per INCOME-DESIGN's
build order step 1:
- `classify_inflow` extracted: the tagging endpoint (`PUT
  /api/transactions/<id>/classify`), submission criterion straight from
  the registry (`direction='in'` only), `income_type` validated against
  INCOME-DESIGN's seven-value vocabulary (`actions.INCOME_TYPES`). Route
  response merges `direction`/`income_type` on top of the existing
  `txn_to_json` shape rather than extending that helper — the listing
  JSON stays byte-pinned to v1.0. Zero-diff gate (no verb creates
  `direction='in'` rows yet, so nothing existing is touched); suite at
  100 tests.
- Rules engine extracted: `create_income_rule` (conflict check against
  existing enabled rules, at-least-one-match-criterion, integer-cents
  bounds, active-member `set_paid_by`), `set_rule_enabled` (no delete —
  disabled rules keep history and drop out of matching), `apply_rules`
  (priority-ordered first-match-wins over unclassified inflows, `dry_run`
  per AGENT-DESIGN's preview-first pattern, `hit_count` observability,
  batch audit row skipped entirely on a no-op match). `match_account` is
  parsed off `external_id`'s `simplefin:<account>:<txn>` convention — the
  only place account identity lives; no schema column exists for it.
  Zero-diff gate 2879986→da30f5f; suite at 123 tests.

Sync flip landed (July 22, 2026), INCOME-DESIGN build-order step 2:
`simplefin_sync.py`'s `amount >= 0: skip` branch is gone. Money in now
inserts through `record_transaction` (extended, not duplicated) with
`direction='in'`: no share fields regardless of what's passed, matched
against enabled `income_rules` immediately on insert
(`_first_matching_rule`, the same first-match-wins logic `apply_rules`
uses for backfill) — a match sets `income_type` and, if the rule
overrides the owner, `paid_by`, and bumps `hit_count`; no match lands
`'unclassified'`. Manual UI entry gets the same capability for free,
since it's the same verb. Outflow behavior is unchanged byte-for-byte.

Cross-error audit done before touching sync, not after: `spending_summary`
had no `direction` filter at all — every inflow would have inflated
monthly spend the moment it landed. Fixed (mandatory).
`compute_balance` and `settle_up`'s covered-rows query were already
safe via their splits `INNER JOIN` (inflows never get split rows) but
got an explicit `direction='out'` filter too, as defense-in-depth —
`tests/test_income_isolation.py` manufactures inflows *with* split rows
attached (something `record_transaction` itself never produces) to
prove the explicit filters catch it, not just benefit from splits
happening to be absent today. Zero-diff gate `da30f5f`→`4d311cf`
(seeded data has no inflows, so this checks the code path is inert for
existing data — the isolation suite is what actually exercises mixed
in/out data); suite at 137 tests.

Hardening pass (July 23, 2026) — a routine integrity sweep (now a
standing per-session ritual) came back green, then a deeper audit of the
income code found two latent issues, both fixed:
- Migration #005 was the only non-idempotent migration (raw `ALTER TABLE
  ADD COLUMN` errors on re-run). Converted `.sql` → a guarded `.py`
  (PRAGMA table_info gate per add), matching 002's pattern and hard
  rule 1. Safe to change a committed migration only because no deployed
  DB has applied it (pre-step-0).
- `settle_up` and `mark_bill_paid` left `direction` to the schema
  DEFAULT `'out'` rather than setting it, so the balance's/spend's
  correctness for settlement and bill rows was emergent from a default,
  not stated or tested. Made explicit in both INSERTs (behavior-neutral,
  gate zero-diff) and pinned with tests that flip the row to `'in'` and
  watch the number move. Suite at 142.
- Finding 3, since closed: a manual `POST /api/transactions` could create
  an inflow because `record_transaction` read `direction` from the client
  `data` blob. Promoted `direction` to a verb parameter (default `'out'`,
  like `source`) — the manual route passes only `source='manual'` so a
  client's `"direction"` key is never read; only sync passes
  `direction='in'`. Closes the undesigned path at the source; gate
  zero-diff (real callers unchanged). Manual income entry, if ever wanted,
  becomes a new explicit path rather than this one silently allowing it.

The routine integrity sweep is now mostly automated (July 23, 2026):
`tests/test_architecture.py` enforces invariant 1 (no raw writes to the
nine governed tables outside actions.py/migrations/fixtures, `members`
carved out as a documented `KNOWN_EXCEPTIONS` entry for the pre-verb
setup route) and registry↔code coherence (every actions.py verb appears
in CORE-DESIGN's table). With schema-version coherence already tested
and the derivation tripwire in place, a green suite now *is* four of the
old by-hand checks. Each tooth was verified to bite by temporarily
introducing the violation. The sweep reduces to: run the suite, run the
balance gate (still manual — the crown-jewel arc check), glance at git.
Suite at 145.

Income build step 3 underway (INCOME-DESIGN build-order step 3), one
brick at a time:
- Step 3a — `income_summary` derivation + `GET /api/income/summary`
  (July 23, 2026): the dashboard card's data source and the third named
  read-time derivation CORE-DESIGN specifies. `gross_inflows` (all 'in'),
  `true_income` (paycheck only), `net_cash_flow` (true − the shared
  `spending_summary` total), `savings_rate` (the one display ratio,
  None-guarded on zero income), `unclassified_count` (the tag-me nudge).
  Integer cents, dollars at the JSON edge. New endpoint, not a field on
  `/api/dashboard`, so the byte-pinned v1 surface stays frozen (parity
  green). `income_summary` added to the tripwire's EXEMPT set (the one
  derivation that *should* see inflows), verified load-bearing. Zero-diff
  gate; suite at 156.

- Step 3b — dashboard income card frontend (July 23, 2026): the SPA's
  dashboard shows an income card beside Spent, consuming
  `/api/income/summary` for the month the dashboard resolved to. Headline
  `true_income` (green), signed `net_cash_flow` (green/red), `savings_rate`
  as a percent or "—", a "total money in" row only when gross ≠ true, a
  brass tagging nudge, and an empty state when there are no inflows.
  Verified visually across every state in a throwaway harness rendering
  the real shipped function against `style.css` — which caught a
  subject-verb-agreement bug the Python tests couldn't. Frontend only;
  backend untouched; suite at 156. Nudge-scope question resolved: the
  card uses `income_summary`'s month-scoped count (the card is a month
  view); a *global* tagging-queue count belongs with the Activity feed's
  tag affordance, not here.

Rest of step 3 planned as a 7-increment build (Alta's scope call, Jul 24:
full scope incl. the analytics chart; rule prompt fires on the 2nd
same-type tag with an editable pre-filled match). Ordered:
1. `GET /api/activity` — done (Jul 24). New read-only endpoint: extended
   txn shape (direction/income_type on top of `txn_to_json`, so
   `/api/transactions` stays byte-frozen), `filter=all|spending|income`,
   and a *global* unclassified-inflow count for the nudge/badge. Zero
   derivation/schema change → gate zero-diff; parity green; suite at 164.
2. Activity feed frontend — done (Jul 24). Inflows render distinct (green
   +amount, "money in", green income-type chip / brass TAG chip for
   unclassified); a 3-way All/Spending/Income filter bar; `txnRow`
   branches on direction (missing → outflow, so the dashboard's shared
   use is safe). The dashboard "Recent" list also switched to
   `/api/activity` so inflows aren't mislabeled as spend there. Verified
   end-to-end against a running app with mixed data (all filters; balance
   and Spent still exclude income). Frontend only; suite at 164.
3. Tagging — done (Jul 24). Tap an inflow → `dlg-classify` type-picker
   ("What kind of income?", six types) → `PUT …/classify`; row re-renders
   with a green income-type chip. Tap handler branches on direction
   (`findTxn` resolves the row across activity / dashboard-recent):
   inflows classify, outflows still edit — inflows never open the spend
   dialog. Classified inflows re-open with their type pre-selected
   (mis-tag is fixable). A global "N inflow(s) still need tagging" badge
   tops the Activity feed (jumps to the Income filter); its grammar and
   the card nudge now share `nudgeText` in render.js (one tested string,
   not two). Verified live (tag → chip flips, badge decrements w/ correct
   singular grammar; re-tag; outflow→edit intact). Suite at 165.
4. "Make this a rule?" — done (Jul 26). On the 2nd inflow tagged with a
   given real income_type, the classify route returns a `rule_suggestion`
   (pre-filled `match_desc` from the description, the type just assigned);
   the SPA offers an editable `dlg-rule` → `POST /api/income/rules`. Fires
   exactly once per type (count==2), suppressed when an enabled rule
   already matches the row — the settled auto-rule-aggressiveness call
   (wait for a repeat match; don't nag). Backend is a read-only helper,
   `actions.suggest_rule_after_classify`, kept out of `classify_inflow` so
   that verb stays a pure one-row edit (its docstring already reserved this
   as a UI-layer concern); `ruleSuggestionText` joins `nudgeText` in
   render.js (one tested wording). No schema/derivation change → zero-diff
   gate 10237b8→547b936; suite 175 python + render.js. Live browser check
   across 1st/2nd/3rd tag, create-rule, and suppression.
5. Refund netting — done (Jul 26). A `direction='in'` `income_type='refund'`
   row subtracts from its category's spend in the month it lands
   (`spending_summary` signed UNION, **no clamp** — a refund can push a
   category/month total negative, the deliberate honest dip). Moved
   `spending_summary` into the tripwire's EXEMPT set (it now reads inflows
   on purpose), bounded to refunds only; the automated coverage that gave
   up is replaced in `test_income_isolation` — refund nets its own
   category/month (incl. the negative dip + cross-month scoping) and EVERY
   non-refund inflow type is proven to leave spend untouched. Netting flows
   into `income_summary` via the one shared spend total (positive test;
   one prior assertion updated to the netted number). Zero-diff gate on the
   refund-free frozen fixture (proves inertness for existing data) **and**
   an enumerated-diff demonstration (`notes/refund-netting-gate-demo.seed.json`)
   showing exactly one diff — that month's total reduced by the refund
   amount, into the negative, nothing else. Live end-to-end: tagging a
   $2,964.43 inflow Refund dropped July Spent $2,610.92 → −$353.51, balance
   untouched. Suite 175→181. Depends on refunds being categorized via the
   existing edit flow (no new mechanism).
6. Income trend derivation — done (Jul 26). The deferred trailing-window
   form. `_monthly_series(db, metric_fn, months_back, anchor)` is the
   reusable trend engine increments 7–16 ride — maps ANY per-month
   `metric_fn` over a window ending at `anchor` (default: latest data
   month, clock-free), empty months zero-filled for a continuous chart
   axis; private on purpose (takes a callable, so not a tripwire aggregate).
   `income_trend(db, …)` = `_monthly_series` over `income_summary`, so each
   month means exactly what the card does and refund netting flows through;
   EXEMPT in the tripwire. `GET /api/income/trend` (anchor default
   `current_period()`, `months_back` clamped 1..24, dollars at the edge;
   v1 surface frozen). Engine tested with a trivial metric_fn to prove it's
   content-blind (the reuse property). Zero-diff gate; suite 181→198.
   (Also: `test_architecture` scan now skips `.claude/`/`venv/` — a harness
   worktree is a full repo copy whose nested `tests/` dodged the dir
   exclusion; committed separately.)
7. Analytics tab + income-vs-spend chart — done (Jul 27). New nav tab
   consuming `GET /api/income/trend` (trailing 6-month window ending at the
   viewed month; month-prev/next shift the window). Hand-rolled SVG, no
   chart lib (CSP + no build step): a stacked bar per month — neutral
   `spent` base + green `saved` cap (the shaded gap) + red `over` cap when
   spend exceeds income — so surplus, deficit, and refund-month (negative
   net spend → all-green) all render from one geometry. Pure helpers in
   `render.js` (`trendBars` geometry, `trendSummary` window aggregate,
   `shortMonth`, `incomeTrendChartHTML`), unit-tested in the
   `node tests/test_render.js` seam (26 checks; every sign case + empty
   state), no `fmt` change (kept clear of the running format task).
   Frontend only — no gate; suite 198 python + render seam. Live browser
   check across the real chart, refund netting (a tagged refund shows July
   all-green), zero-fill (empty months keep their axis label), window
   navigation, and the per-window savings-rate headline.

Deeper-analytics extensions (planned Jul 26, 2026, post-step-3; Alta's
selected set from a recommendation menu). These ride the two primitives
that steps 6–7 build — the shared `monthly_series(db, metric_fn,
months_back)` month-bucketing engine (build it *inside* inc 6, don't
one-off it) and the analytics tab's SVG seam — so each is a small
increment: "pass a different `metric_fn`," gated zero-diff, tripwire
covering the income-isolation filter automatically. Architectural
throughline: build the trend engine once, then these grow cheaply. Order
is by leverage (cheapest-onto-the-new-tab first):

Tier A — pure read-time derivations, no schema, zero-diff gate:
8.  Category trend — done (Jul 27). `derivations.category_trend(db,
    category, months_back, anchor)` rides `_monthly_series` over
    `spending_summary` (the "pass a different `metric_fn`" payoff), + a
    trailing 3-mo rolling average (`round_ratio`, integer cents, ties-even,
    warms up over 2 months) + exact MoM delta (None first). Refund netting
    flows through per-category; EXEMPT in the tripwire (reads refunds via
    `spending_summary`). `GET /api/analytics/category-trend` (new
    `/api/analytics` namespace; category required, anchor/months_back like
    the income trend, dollars at the edge). **Backend only** — the
    category-trend *visuals* are deferred to a batched analytics-tab
    frontend increment (kept clear of `render.js` while the negative-format
    task edits it). Zero-diff gate; suite 198→210.
9.  Savings-rate trend — done (Jul 27). `derivations.savings_rate_trend`
    reuses `income_trend` (no aggregate recomputed — per-month rate is the
    card's exactly) and layers a trailing 3-month **rolling** savings rate:
    cumulative Σ net_cash_flow ÷ Σ true_income (weights by income, not an
    average of ratios), which smooths the single-month noise. Non-redundant
    on purpose — the raw per-month rate already lives in `income_trend`, so
    the rolling rate is the reason this exists. EXEMPT like `income_trend`;
    `GET /api/analytics/savings-rate-trend` passes ratios through (not
    money), null on zero income. Backend only. Zero-diff gate; suite
    214→223.
10. Category mix + top merchants — done (Jul 27).
    `GET /api/analytics/spending-composition` returns a month's total, by_
    category with a `share` (= amount/total computed at the edge over
    `spending_summary`, so shares reflect refund netting — NOT a new
    aggregate), and `top_merchants`. `derivations.top_merchants(db, month,
    limit)` IS new — outflows grouped by description ('who did we pay the
    most?'), outflows only, settlements excluded, deliberately not
    refund-netted (different axis). Reads outflows only, so NOT exempt — the
    tripwire proves it ignores inflows. Money `{cents, display}`. Zero-diff
    gate; suite 233→242.
11. Per-member view — done (Jul 27). `derivations.member_breakdown(db,
    month)`: per active member, paid (fronted shared) vs owed (basis-point
    share) vs net; nets sum to zero, `round_ratio` per row like
    `compute_balance`. Shared outflows only → NOT exempt; a
    `test_income_isolation` case proves the `direction='out'` filter guards
    paid/owed from a mis-split inflow (same bar as `compute_balance`).
    `GET /api/analytics/member-breakdown`; money `{cents, display}`.
    Zero-diff gate; suite 242→251.
12. Bill-vs-actual variance — done (Jul 27). `derivations.bill_variance(db,
    period)`: per active bill, defined `bills.amount_cents` vs actual (the
    `bill_payments`→`transactions` amount) vs variance (actual − defined;
    +over); unpaid → None. Outflows only → NOT exempt.
    `GET /api/analytics/bill-variance`; money `{cents, display}`, null for
    unpaid. Zero-diff gate; suite 251→259. **Completes Tier A (#8–12).**

Tier B — needs a heuristic, still no schema change:
13. Recurring-charge / subscription detection — cluster outflows by
    (normalized description, ~amount, ~monthly cadence); a *suggestion*
    surface, not an authority (same honesty as INCOME-DESIGN's refused
    transfer auto-pairing — a coincidence must not silently become a
    "subscription").
14. Cash-flow forecast — project end-of-month / next-month position from
    recurring income (paycheck `income_rules` encode cadence+owner),
    recurring bills, and scheduled goal contributions. Where rules, bills,
    and goals finally combine into one forward-looking number; the natural
    bridge into step 7 (the MCP assistant).
15. Anomaly flags — "category X is N% above its trailing 3-mo average"; a
    threshold on top of #8, surfaced passively in the activity feed.
16. Goal pace / projection — completion-date projection at current
    contribution rate over `goal_contributions`.

Deferred (Tier C, its own designed feature — NOT smuggled inline):
category budgets / envelopes. Explicitly out of scope in INCOME-DESIGN;
needs a `budgets` migration + `set_budget` verb + a `budget_status`
derivation. Take it on as its own design increment when wanted, not as a
quick analytics add.

Income build step 3 is now complete (increments 1–7 all done): the whole
income/classification feature ships — sync flip, dashboard card, activity
feed, tagging, auto-rules, refund netting, income trend, and the analytics
chart.

**CORE-DESIGN step 7 — the assistant — started (Jul 27).** Design
discussion held (see AGENT-DESIGN): much of its "build order" is already
done (audit_log, one-write-path verbs, INCOME-DESIGN 1–3, and the read
derivations that make "the agent does no math" true).

**DOOR DECISION MADE (Jul 27) — two doors on one shared read layer:**
- **Charlee → in-app "Ask" tab.** A Flask route runs an Anthropic tool-use
  loop over the read functions IN-PROCESS under the existing session login —
  no MCP, no bearer token, no Funnel, nothing new exposed. Reopens
  AGENT-DESIGN's "no embedded chatbot" line *deliberately*: Charlee is
  non-technical, phone-first, and barely uses claude.ai, so an in-app chat
  beats a claude.ai connector. Cost accepted: an Anthropic API key on the Pi
  + modest per-query billing. Satisfies "one write path" *better* than MCP
  (it reuses the same verbs the UI does). Needs a chat UI in the SPA.
- **Alta → tailnet-only MCP server.** FastMCP sibling process wrapping the
  read endpoints over HTTP with a bearer token; connect from Claude
  Code/Desktop over Tailscale. **No Funnel / no public exposure** — the one
  option with public exposure is off the table.
Recommended build order (MCP-first, to get a working assistant fast that
de-risks the shared tools before Charlee's UI, and needs no Anthropic key):
`api_tokens` + bearer auth ✅ → **MCP read tier ✅ (deployed to Pi, Alta
soaks it over Tailscale)** → **in-app Ask endpoint + chat UI (Charlee) ←
SCOPED, building** → two-phase write tier. Token
identity = **per-person** (decided). Still pending: income-visibility
policy (enforce at the API), and the write-tiering ratification (classify
direct, rules two-phase — due at the write tier). Prereqs Alta must supply:
an Anthropic API key (for in-app) and their MCP client over Tailscale.
- Auth foundation done (Jul 27): migration #006 `api_tokens` (v5→v6) +
  bearer auth. Per-person revocable tokens, SHA-256-hash-only storage,
  plaintext returned once; `create_api_token`/`revoke_api_token` verbs
  (registered; `api_tokens` in GOVERNED_TABLES); `find_active_api_token`
  auth helper bumps `last_used_at`. `login_required` now accepts session OR
  bearer, **scope enforced by HTTP method** (GET=read, mutating=write);
  `ui_actor` → `mcp:<label>` for tokens. Token mgmt routes
  (`POST/GET /api/tokens`, `.../revoke`) are `session_required` (a token
  can't mint tokens); mint issues `'read'` only until the write tier.
  Enumerated gate (notes/006): api_tokens + schema_version bump, nothing
  else. Suite 259→277. Live-verified: bearer read 200, bearer write 403,
  bearer-mint 401.
- **MCP read tier built (Jul 27)** — `ledger_mcp.py`, the FastMCP sibling
  process (AGENT-DESIGN build-order step 2). Holds no state, does no math: a
  thin `httpx` client of the Flask read API under a **`read` bearer token**
  (`api_get` maps 401→"issue a new token", 403→lacks scope, other 4xx→the
  API's own message). 13 read-only tools wrapping every read endpoint —
  `ledger_household_snapshot` (start-here), `_balance`,
  `_spending_composition`, `_category_trend`, `_income_summary`,
  `_income_trend`, `_savings_rate_trend`, `_member_breakdown`,
  `_bill_variance`, `_list_income_rules`, `_unclassified_inflows` (search
  wrapper), `_search_transactions` (evidence), `_list_goals_and_bills`.
  Docstrings ARE the product (units-twice, true_income≠gross_inflows, "search
  ≠ totals"). Serves over streamable HTTP; `deploy/ledger-mcp.service`
  (systemd, Requires=pifinance), `.env` vars (`LEDGER_MCP_TOKEN`/`_API_BASE`/
  `_HOST`/`_PORT`), and `deploy/mcp-read-tier.md` (mint→deploy→`claude mcp
  add` over Tailscale). Deps: `mcp>=1.2`, `httpx>=0.27`. **No schema/
  derivation change → no balance gate** (pure HTTP client of already-gated
  endpoints, like the frontend increments); safety net is
  `tests/test_ledger_mcp.py` — each tool's JSON proven byte-equal to the
  Flask endpoint it wraps (can only reshape, never recompute), driven through
  FastMCP dispatch over an httpx WSGITransport at the real app. Suite 277→289.
  End-to-end smoke-verified: real streamable-HTTP client lists 13 tools and
  reads live snapshot/search through the running app.
  **Charlee's Ask tab — scoped (Jul 29, 2026)**, decisions settled with
  Alta (full plan in AGENT-DESIGN "Ask tab — v1 build plan"): read-only
  Q&A, model Haiku 4.5, send-and-wait UX, one shared read-tool spec that
  both `ledger_mcp` and the in-app loop consume (no docstring drift),
  client-side history, full income visibility (current default). Build in a
  tool-loop-round cap + Anthropic prompt caching. Increments: (1) shared
  read-tool registry + bounded loop harness, tests via a MOCKED Anthropic
  client + endpoint-parity (no key/live calls in tests); (2) `POST /api/ask`
  (session_required, model+key from env, vocabulary system prompt); (3) the
  "Ask" SPA tab (chat UI, render.js helpers, node-seam tests). Read surface
  → no schema/migration/gate. Prereqs Alta supplies: `ANTHROPIC_API_KEY` in
  the Pi `.env` (only for live test + deploy — inc 1 & 3 need no key) and the
  `anthropic` SDK in requirements.
- **Ask tab increment 1 — done (Jul 29, 2026).** `agent_read_tools.py`: the
  ONE read-tool surface both doors consume — all 13 tools' name/description/
  input_schema in one place (`DESCRIPTIONS` is the single source), plus
  `call_read_tool(getter, name, args)` routing each tool to its real read
  endpoint via an injected getter (reshape only, never recompute) and
  `anthropic_tools()` (Messages-API format, prompt-cache breakpoint).
  `ask_loop.py`: `run_ask`, the bounded tool-use loop (client + getter
  injected, round cap, tool errors caught + recoverable, read-only).
  `ledger_mcp` refactored to import `DESCRIPTIONS` (docstrings dropped; typed
  params still drive its schemas) so the two doors can't drift — a test
  asserts its live tool descriptions equal `DESCRIPTIONS`. Loop tested against
  a MOCKED Anthropic client (no key/live calls); registry tested over a
  seeded app. `anthropic>=0.40` added (runtime-only). Suite 289→300; read
  surface, no gate.
- **Ask tab increment 2 — done (Jul 29, 2026).** `POST /api/ask` in `app.py`:
  `session_required` (NOT bearer — a read token must never trigger paid API
  calls), reads message + client-held history, runs the loop, returns
  `{answer, tools_used, rounds, stopped}`; 503 when no key, 400 on empty. The
  plumbing lives in `ask_loop.py`: `answer()` (client injectable — tests pass a
  mock, prod builds it from env), `make_app_getter(app, user_id)` (in-process
  getter running the app's read endpoints under the caller's session via a test
  client — no HTTP/token), `system_prompt(period)` (the vocabulary rules the
  model reads), and a LAZY `_make_client` (app imports fine with no SDK).
  5 route tests via a mocked client (loop runs+answers, empty→400, no-key→503,
  no-session→401, bearer→401); suite 300→305. `anthropic` installed. Live
  smoke-verified end-to-end against the REAL Anthropic API (`ask_smoke.py`,
  untracked, synthetic data): "is rent paid?" → correct warm answer quoting
  the display string, real tool call — proving the SDK response shape matches
  the loop.
- **Ask tab increment 3 — done (Jul 29, 2026).** The SPA "Ask" tab: a new
  nav tab, a phone-first send-and-wait chat. `askThreadHTML(messages, pending)`
  in `render.js` (pure: brass user bubbles, dark bot bubbles, escaped content,
  animated thinking dots, an empty state with example-question chips); `state.
  ask` holds client-side history; `renderAsk`/`askSend` in `app.js` POST to
  `/api/ask` with `{message, history}` and re-render (history = the turns
  before the question). Wired in `wireMain` (submit, example chips, scroll-to-
  latest, refocus). 4 node-seam render checks (35→39); full suite 305. Visual
  pass against `style.css` in a harness across empty/conversation/pending —
  which caught a real cascade bug (`form > .btn.primary { width:100% }`
  squashed the input; fixed with a higher-specificity `.ask-bar .btn.primary`).
  Frontend only, no gate. **Ask tab v1 is feature-complete (inc 1–3).**
  `ask_smoke.py` (untracked) is the local live-check.
- **Ask tab DEPLOYED to the Pi (Jul 31, 2026).** Advanced `main` to rework's
  tree via `--no-ff` merge `1c5a27e` (fast-forward push); `deploy/deploy.sh
  origin/main` on the Pi → zero-diff `GATE PASS` (no migration; `pip install`
  pulled the `anthropic` SDK); `ledger-mcp` restarted for the shared-desc
  refactor. Verified over the tailnet: `POST /api/ask` now 401 (was 404, live +
  session-gated), served `render.js`/`app.js` carry the Ask tab, and the MCP
  server is back up with 13 tools on the refactored descriptions. Rollback
  backup `finance.db.bak-2026-07-31-201628`. **CONFIRMED LIVE (Jul 31):** the
  in-app Ask tab answered a real question in the app — `ANTHROPIC_API_KEY` is
  set on the Pi and the whole path works end to end. **BOTH USERS LIVE (Jul 31):**
  Charlee's Tailscale device-share is set up (her own account, Alta shared just
  the Pi node) and she reached the app + Ask tab on her phone. Key note: the
  `ANTHROPIC_API_KEY` on the Pi expires in ~30 days (set Jul 31) — Alta will
  mint a fresh one then (same billing/credits carry over).

**CORE-DESIGN step 7 read+chat surface is DONE and live:** `api_tokens`/auth →
MCP read tier (Alta, Tailscale) → in-app Ask tab (Charlee) — all deployed and
proven on the Pi, both users onboarded. **The two-phase write tier is now
SCOPED (Aug 1, 2026)** — the remaining step-7 work, planned in AGENT-DESIGN
"The write tier — v1 build plan". Key finding: the four write verbs
(`classify_inflow`, `create_income_rule`, `set_rule_enabled`, `apply_rules`)
already exist and already run over bearer; this tier is the two-phase
choreography + write-scope token + MCP tools around them, not new verbs.
Decisions settled with Alta: **write tiering ratified as designed** (classify +
set_rule_enabled direct/logged; create_rule + apply_rules two-phase),
**MCP-only this tier** (Ask-tab write deferred), **`also_apply_to_existing` =
new-rule-only** (confirm reclassifies just the new rule's matches, so the
preview count == what changes). Four increments: **(A) done (Aug 1, 2026)** —
migration #007 `pending_actions` (schema_version 6→7), the table into
`GOVERNED_TABLES`, `propose_action`/`confirm_action` rows added to CORE-DESIGN's
registry first (verbs land in B), `REQUIRED_SCHEMA_VERSION` 6→7; enumerated-diff
gate PASS (notes/007: pending_actions=0 + schema_version bump, nothing else);
suite 305 python + 39 render, green. Not yet deployed (deploy is inc D, with
`#007 --live`). **(B) done (Aug 1, 2026)** — `propose_action`/`confirm_action`
in `actions.py` (propose validates + dry-runs + parks a frozen payload;
confirm claims the pending row `pending→confirmed` FIRST then dispatches, so a
re-confirm never double-executes; create_rule confirm applies **new-rule-only**
via `_apply_single_rule` so the effect equals the previewed count) + thin
routes `POST /api/actions/propose` and `POST /api/actions/confirm`
(`login_required`, write scope for bearer). `_validate_income_rule` extracted
so propose+create share one validator; `_write_matches`/`_matching_pass(rules=)`
factored out of `apply_rules`. No schema/derivation change → **zero-diff gate
PASS** (v7 source, ca3b9a7→cec35b1); suite 319 python + 39 render. **(C) done
(Aug 1, 2026)** — `read,write` token minting: `POST /api/tokens`'s hardcoded
`"scopes":"read"` lifted to a caller-chosen `data.get("scopes","read")` (the
`create_api_token` verb already validated the value; default stays `read`).
No token UI exists — tokens are minted via `curl POST /api/tokens` (per
`deploy/mcp-read-tier.md`), so this is backend-only; that doc's stale
"read-only until the write tier" note corrected. Tests: mint `read,write` →
can POST `/api/actions/propose` (201); default `read` → 403; unknown scope →
400. No schema/derivation/data change → no gate; suite 322 python + 39 render.
**(D) done (Aug 1, 2026)** — five MCP write tools in `ledger_mcp.py` over an
`api_write(method, path, body)` helper (401→reissue / 403→needs `read,write` /
4xx→verb's message): DIRECT `ledger_classify_inflow` + `ledger_set_rule_enabled`,
and TWO-PHASE `ledger_propose_income_rule` / `ledger_apply_rules` →
`ledger_confirm_action`. Server instructions/docstring updated (writes exist,
user-confirmed, propose→preview→yes→confirm; settle/edit/delete/money-movement
still absent). Write-tool descriptions live in `ledger_mcp` (MCP-only — Ask
loop stays read-only), so the shared-registry drift test is scoped to read
tools and the read-registration test relaxed to a subset. `tests/test_ledger_mcp_write.py`
(7 tests, WSGITransport seam, read,write token, each write's effect checked in
the db + single-use through dispatch + a `read` token proven 403 on a write
tool); pure HTTP client of gated endpoints → no balance gate; suite 329 python
+ 39 render. Tool surface: 18 total (13 read + 5 write). **The two-phase write
tier is CODE-COMPLETE (inc A–D); CORE-DESIGN step 7 is fully built.**
- **DEPLOYED TO THE PI + verified live (Aug 1, 2026).** Advanced `main` to
  rework's tree via `--no-ff` merge `74e637f` (first parent = prior main
  `1c5a27e`, fast-forward push); `deploy/deploy.sh origin/main` on the Pi →
  **GATE PASS** (no money moved; enumerated structural diff only —
  `pending_actions` None→0 + `schema_version` 6→7), migration `#007` applied
  `--live`, `pifinance` restarted clean. Rollback backup
  `finance.db.bak-2026-08-01-184437`. Minted a `read,write` token (per-person,
  via curl login→`POST /api/tokens` with `scopes:"read,write"`), set it as the
  Pi's `LEDGER_MCP_TOKEN`, restarted `ledger-mcp`. Verified over the tailnet:
  Flask write endpoints now 401 (were 404); the MCP server exposes all **18
  tools** (5 write); and a live phase-1 `ledger_apply_rules` propose succeeded
  under the deployed token (**not** 403 → the token is genuinely `read,write`),
  parking an auto-expiring no-op pending action (rows_affected 0) — the full
  read→write→two-phase path is proven end to end on real data.
  **CORE-DESIGN step 7 (the assistant) is now COMPLETE and live: read tier,
  in-app Ask tab, and the two-phase agent write tier all deployed.** (Note: a
  connected MCP client must reconnect to re-list the 5 new write tools; the
  server serves them regardless.) (Note: the propose endpoint is `/api/actions/propose`, generic
over `action_type`, not the rules-specific path the scope note first sketched —
one propose path serves both create_rule and apply_rules.)
Flagged build frictions: `confirm_action` can't wrap the dispatched verb (it
opens its own `BEGIN IMMEDIATE`) → mark-confirmed-first then dispatch;
compound confirm is create + scoped-classify, two atomic sub-calls. Prereq:
Alta mints a `read,write` token (inc C) and repoints the MCP client.
Lighter alternatives if a read feature is preferred instead: analytics Tier B
(#13–16: recurring-charge detection, cash-flow forecast, anomaly flags, goal
pace) or the still-open income-visibility policy. Untracked dev tools left in
the tree on purpose: `ask_smoke.py` (live-checks `POST /api/ask`) and
`soak_local.sh` (local MCP soak).

- **Mobile bottom-nav fix — DEPLOYED (Aug 1, 2026).** The SPA tab bar's six
  tabs (Home/Activity/Bills/Goals/Analytics/Ask) overflowed on a phone: the
  mobile `.tabbar` was `display:flex; justify-content:space-around` with
  non-shrinking padded buttons, so under `nowrap` the last tab (Ask) was
  pushed off the right edge and couldn't be tapped. Fixed in `static/style.css`
  — `.tabbar button` → `flex:1 1 0; min-width:0` (equal-width, shrink-to-fit,
  so the row always divides the bar evenly and no tab can overflow), then the
  labels sized to 10px/no-tracking (iOS's own tab-label size) so the longest,
  "Analytics", shows in full; the ellipsis stays only as a guard for
  extreme/ancient (≤320px) screens. Desktop `topnav` untouched. Frontend-only
  → no gate; render seam still 39 checks. Shipped in two deploys (the fix,
  then the label sizing): `main` `74e637f`→`d751c35`→`3984f35` (each a
  `--no-ff` merge, first parent = prior main, tree identical to rework;
  `deploy/deploy.sh origin/main` each → **GATE PASS zero-diff**, no
  migration). Verified live on Alta's phone after a hard-refresh: Ask
  reachable, Analytics full. Note: the in-app Browser tool was wedged this
  whole session (repeated 300s navigate timeouts), so the visual checks fell
  back to code analysis + a throwaway CSS harness + on-device confirmation
  rather than a screenshot.

- **Ask-tab write (tagging) for Charlee — SCOPED (Aug 2, 2026).** The chosen
  next increment now that step 7 is complete: extend Charlee's in-app Ask loop
  from read-only to letting her TAG inflows by chatting (her door; the MCP
  write tier is Alta's). Plan in AGENT-DESIGN "Charlee's Ask tab — write
  (tagging) build plan". Decisions settled with Alta: **tagging only**
  (`classify_inflow`; rules two-phase deferred), **conversational confirm** (no
  card — reversible + logged), **model stays Haiku 4.5**. Key facts: the loop
  (`ask_loop.py`) and the write verbs/routes already exist, and the Ask
  session already carries write scope — so the write goes through the same
  `PUT …/classify` route the SPA uses (invariant 2), attributed to
  `ui:<charlee>`; dangerous verbs stay omitted; the request/response boundary
  is a natural human-confirm gate. Three increments: (1) `make_app_caller`
  in-process POST/PUT + a one-entry write-tool surface (kept out of the
  read-only shared registry) + loop routing, mocked-client tests assert the
  db row flips; (2) `POST /api/ask` write-enabled + updated system prompt; (3)
  Ask-tab "tagged ✓" UI feedback. No schema/migration/gate (existing verb).
  Prereq: `ANTHROPIC_API_KEY` already on the Pi (⚠ expires ~Aug 30, 2026).
  - **Inc 1 done (Aug 2, 2026).** `agent_write_tools.py` — one-tool surface
    (`ledger_classify_inflow`), kept out of the read-only shared registry,
    executing via an injected caller against the same `PUT …/classify` route
    the SPA uses. `ask_loop.py`: `make_app_caller` (in-process POST/PUT under
    the caller's session, 4xx→recoverable tool error) + `run_ask` gains a
    `caller` param — when given, the write tools are appended (one prompt-cache
    breakpoint on the combined block) and their `tool_use` routes to the caller;
    `caller=None` stays byte-unchanged read-only. **The live route is still
    read-only — enabling it is inc 2.** `tests/test_ask_write.py` (4, mocked
    client, no key): the tool actually flips `income_type` through the route +
    logs as `ui:avery`; write tools appear only with a caller (14 vs 13); a bad
    id and an outflow are both caught (the verb's inflow-only criterion holds).
    Suite 329→333 python + 39 render; no gate.
  - **Inc 2 done (Aug 2, 2026).** `answer()` now passes `make_app_caller` into
    `run_ask`, so `POST /api/ask` is write-enabled; the system prompt's "you can
    look but not change anything" became the tag-after-they-tell-you rule (only
    `classify_inflow`, never guess, confirm after; still no money-movement /
    settle / rules / edit / delete). Module + route docstrings updated.
    `test_ask_route`: a classify `tool_use` through the route really flips
    `income_type` + logs as `ui:avery`, write tool offered (14), prompt grants
    tagging. Suite 333→334 python + 39 render; no gate. **The feature is
    functionally complete once deployed** — the chat's text reply ("Tagged it
    as your paycheck ✓") IS the confirmation.
  - **Inc 3 done (Aug 2, 2026).** UI honesty + a write signal: `askThreadHTML`
    shows a subtle "✓ tagged" chip on an assistant reply that actually tagged
    an inflow (message carries `tagged`, set in `askSend` from `tools_used`;
    stripped from the history sent back to the model). Fixed the stale copy —
    the empty-state sub and the ask-note both claimed the assistant "never
    changes anything" / is "read-only"; now they say it can tag a deposit (other
    changes still in the app). `.ask-tagged` chip in style.css. Frontend only;
    render seam 39→41; no gate. **Ask-tab write (tagging) is feature-complete
    (inc 1–3).** Visual check of the chip fell to the render seam + code (in-app
    Browser tool still wedged). Deferred later: rules two-phase in chat,
    streaming, conversation persistence.
  - **DEPLOYED TO THE PI (Aug 2, 2026).** Advanced `main` to rework's tree via
    `--no-ff` merge `6d0414f` (first parent = prior main `3984f35`, fast-forward
    push); `deploy/deploy.sh origin/main` → **GATE PASS, zero diff, no
    structural changes** (frontend + route only, no migration; `pip install` a
    no-op — `anthropic` already present), `pifinance` restarted clean (no
    `ledger-mcp` restart — this doesn't touch the MCP server). Rollback backup
    `finance.db.bak-2026-08-02-224128`. Verified over the tailnet: served
    `/render.js` carries the `ask-tagged` chip + "tag a deposit" copy (stale
    "never changes" gone), `/app.js` reads `tools_used`→`tagged` (stale
    "Read-only" gone), `/style.css` has `.ask-tagged`; app healthy. **Charlee
    can now tag inflows by chatting in the Ask tab** — write path is
    session-based and live wherever `ANTHROPIC_API_KEY` is set (⚠ expires
    ~Aug 30). **CONFIRMED LIVE (Aug 2, 2026): Charlee tagged a real deposit
    through the Ask tab on her phone** — the full chat→tag path works end to end
    for the priority user. Ask-tab tagging is DONE and in use.

**UI REDESIGN — "Garden" (started Aug 2, 2026).** Charlee wants a full visual
redesign: modern, app-like, organic. Direction chosen = **the Garden** — warm,
growth-themed, rounded, shipping with **both light and dark** (daylight garden /
night garden are one design's two themes, not two designs). Alternate "Refined"
direction set aside. Concept artifacts (plain-HTML mockups, shareable with
Charlee): two-directions compare w/ live light-dark toggle
`claude.ai/code/artifact/7558c5b2-5be2-480a-9b7b-85e6a0d39cb2`; single dashboard
`…/8e42a827-315c-4b30-9815-92697cf12af9`.
- **Build path decided (Alta): restyle the EXISTING vanilla app in-place first;
  framework migration DEFERRED to a later phase** (do it once the look is
  validated + when richer interactions actually demand it — not both at once,
  which would be the big-bang rewrite the project refuses). So CORE-DESIGN's
  "no framework migration" line STANDS for now; amend it only at the framework
  phase. Backend/verbs/gate/balance: untouched — this is frontend-only, ships
  through the zero-gate frontend deploy, fully reversible.
- Phased plan: **(1) design-token foundation + light/dark** — DONE (Aug 2):
  the whole app is token-driven (~15 CSS vars), so redefining `:root` +
  `@media (prefers-color-scheme)` + `data-theme` overrides shifts every screen
  to the Garden palette + rounded type + softer radii at once; `--mono` swapped
  to system rounded (dropped the IBM Plex Mono Google-Fonts link — one fewer
  external dep), `--radius` 12→20, FAB shadow softened, `theme-color` metas per
  scheme. Suite 334 + 41 render green; no gate (CSS/HTML only). NOT deployed yet
  — holding the deploy until the signature components land so Charlee's first
  sight is the coherent Garden, not a half-reskin. **(2) signature components —
  DONE for the dashboard (Aug 2):** income savings ring (`incomeCardHTML` →
  conic ring, seam-tested, commit a7b66b5); balance hero (`beamHTML` → green
  `.balance-hero` card, which also dropped its state-coupling); goal growth
  vessels; floating rounded Garden nav pill + rounded-square FAB (f5c6ee3).
  render seam 41 + suite 334 green; frontend-only, no gate. The Garden
  dashboard is now coherent. **IA note deferred:** the mock's 5-tabs-+-center-+
  nav is an information-architecture change (real app has 6 tabs) — kept 6 tabs
  + separate FAB for now; consolidating is a UX call for Alta/Charlee later.
  **(3)** sweep the other tabs (Activity/Goals/Bills/Analytics/Ask) for
  per-screen polish; **(4)** motion + organic touches (ambient blobs,
  transitions, growth animations). Since the in-app Browser tool is wedged,
  visual sign-off is on the real device after deploy (a synthetic preview is
  both heavy and less faithful than the real app given the token-driven CSS).
  **Decision (Aug 3): Alta wants the app modeled EXACTLY on the Garden mock**
  (artifact `7558c5b2…`). Built + deployed the exact match in pieces, each a
  clean frontend deploy (GATE PASS zero-diff): **inc 2b** greeting header +
  member avatars + ambient drifting blobs + a manual ☀︎/☾ theme toggle
  (data-theme + localStorage, overrides prefers-color-scheme); **inc 2c**
  theme CROSSFADE (View Transitions), the mock's 5-slot mobile nav
  (Home·Activity·[+]·Goals·Ask, elevated honey center + = add txn; Bills +
  Analytics moved to two Home "shortcut" pills via data-goto; desktop keeps the
  6-tab topnav; FAB desktop-only), green active tab, "Coming up"/"Growing
  toward" copy, goal 🌱 + caption, green category bars; **inc 2d** rounded emoji
  icon-tiles on the bills/recent lists via a shared seam-tested `catEmoji()`
  (💵 for money-in). **The Garden dashboard now matches the mock**; the only
  unbuilt mock element is the "▲ vs last month" pill (needs a prior-month
  figure `/api/dashboard` doesn't return — deferred, minor). render seam 42 +
  suite 334 throughout. Light/dark is a manual toggle OR follows the phone.
  **(3) done (Aug 3):** Garden-polished the other tabs — Bills rows get emoji
  icon-tiles + Goals get the 🌱 sprout (Activity already inherits icon-tiles via
  txnRow); the All/Spending/Income filter → rounded pills w/ green active, month
  switcher rounder, tag banner honey-pill; badges → soft-filled chips (cascades
  to income/tag chips + bill badges). Analytics/Ask were already token-themed.
  render seam 42 + suite 334. **(4) done (Aug 3):** growth motion — category
  bars / goal vessels grow up (scaleX) on render, the savings/income ring's
  conic fill animates 0→rate via a registered `@property --p`, balance hero +
  cards get a soft fade-rise; all replay on render, all off under
  prefers-reduced-motion (guard now kills animations too). **The Garden redesign
  (phases 1–4) is complete and deployed** — whole app, light+dark w/ crossfade,
  dashboard matches the mock, other tabs polished, growth motion. **The "vs
  last month" spent pill is done too (Aug 3)** — a seam-tested `vsLastMonth()`
  reads the existing `/api/income/trend` 2-month window on the frontend (no
  backend change; up=clay/down=green, null when no baseline), so the dashboard
  now matches the mock with nothing outstanding. Framework migration still
  deferred (CORE-DESIGN's no-framework line stands) — the one remaining
  optional future, if/when richer interactions demand it.

**NEW DIRECTION — Household Inventory ("the pantry"), SCOPED (Aug 3, 2026).**
Alta wants to go beyond finance: track the ~20–30 household staples they don't
want to run out of + a shared shopping list, so they don't buy twice or run
low. Design doc written: `docs/INVENTORY-DESIGN.md` (governs, checked against
CORE-DESIGN). Thesis: Ledger is uniquely placed because it already has the two
things standalone inventory apps lack — the **bank feed** (purchases = what
enters the home, for eventual self-population) and the **Ask assistant with
write access** (upkeep by conversation). Discipline: curated staples, NOT
exhaustive (exhaustive is the upkeep trap). Recommended MVP + settled defaults
(open to redirect): household-scoped (like bills/goals), ONE `items` table with
a 3-state `status` (stocked/low/out, NO quantities), the shopping list is a
DERIVATION not a stored table, one-offs archive themselves when bought, chat is
a first-class input from day one (add_item/set_item_status as direct writes
like classify_inflow). Deferred: purchase auto-population, restock prediction,
barcode, quantities, money tie-in. First non-finance domain → widens Ledger's
identity to "the shared household" (money invariants untouched — inventory
never touches money). Build order: **#008 items migration → verbs + derivations
(shopping_list/low_stock) + endpoints → Garden Inventory SPA view (Home
shortcut pill, 5-slot nav unchanged) → chat input → later inference/prediction.**
- **Inc 1 done (Aug 3, 2026).** Migration #008 creates the empty `items` table
  (schema_version 7→8): household staples + one-off needs, 3-state `status`
  (stocked/low/out), no quantities, soft-delete, `CHECK`-constrained `kind`/
  `status`. `items` into `GOVERNED_TABLES`; add_item/set_item_status/rename_item/
  set_item_note/archive_item rows added to CORE-DESIGN's registry first (verbs
  in inc 2); `REQUIRED_SCHEMA_VERSION` 7→8. **Enumerated-diff gate PASS**
  (notes/008: items=0 + schema_version bump, nothing else — inventory never
  touches money); suite 334 + 43 render. NOT deployed (deploy comes with a later
  inc, `#008 --live`).
- **Inc 2 done (Aug 3, 2026).** Verbs `add_item`/`set_item_status`/`archive_item`
  in `actions.py` (3-state, defaults + validation, audited; a one-off set
  `stocked` = bought auto-archives off the list); derivations `shopping_list`
  (staples low/out + active one-offs, urgent-first) + `low_stock` (staples only)
  — they read `items` not transactions, so the tripwire auto-covers them
  inflow-invariant; endpoints `GET /api/inventory` (items + computed shopping +
  low_count) and POST/PUT/DELETE thin callers (`login_required`, write scope for
  bearer). `test_item_verbs` + `test_inventory_routes` (incl. bearer write-scope
  gating). **Zero-diff balance gate PASS** (v8 source, 8242bd8→HEAD — inventory
  inert for the finance snapshot); suite 334→346 python + 43 render. Still not
  deployed.
- **Inc 3 done (Aug 3, 2026).** The Garden Inventory SPA view. Reached from a
  new **🧺 Pantry** Home shortcut pill (beside Bills/Analytics) + a desktop
  topnav tab (`TABS` grew to 7; the 5-slot mobile nav is deliberately
  unchanged — dedicated slot is a later IA call). Two cards: a derived "Need to
  buy" list (each row a "Got it" check-off = mark stocked; a one-off
  self-archives off the list when bought) and the Staples tracker with a
  tap-to-cycle **stocked→low→out** status chip (green/honey/clay, the badge
  palette), each card closing in a quick-add field; a faint ✕ stops tracking a
  staple. Pure `inventoryHTML(data)` in `render.js` (the analytics-helper
  pattern, node-seam tested); `itemIcon` gives pantry rows a 🧺 fallback + a few
  household keyword icons instead of the money-card glyph. `app.js` wiring is
  thin — cycle/got-it → `set_item_status`, adds → `add_item`, remove →
  `archive_item`, all the inc-2 endpoints (no backend change). Gotcha fixed: an
  input named `name` is shadowed by `form.name`, so the add handlers read the
  input directly. **Frontend only — no schema/derivation/route touched, so no
  balance gate** (like every prior frontend increment); suite 346 python + 43→48
  render. The in-app Browser tool worked this session: verified live end-to-end
  (status cycle drops Milk off the list + lowers the "running low" badge, both
  add fields, one-off archive-on-buy, mobile 5-slot nav + desktop 7-tab
  topnav). Still not deployed.
- **Inc 4 done (Aug 3, 2026).** Pantry chat input — Charlee keeps the pantry by
  talking ("we're out of coffee", "add paper towels", "what do we need?"). No
  new verbs/routes: the tools bottom out in the inc-2 endpoints the SPA uses
  (one write path). **Read surface (shared, both doors):** `ledger_inventory`
  joins the 13 read tools in `agent_read_tools.py` + a `ledger_mcp` wrapper
  (drift test stays green; Alta's MCP gains pantry *visibility* too) — it's the
  "what do we need?" read and how the model finds an item's `id` before
  changing it. **Write surface (Ask-only, Charlee's door):** `ledger_add_item`
  + `ledger_set_item_status` in `agent_write_tools.py` — direct writes like
  `classify_inflow` (logged, reversible, `ui:<name>`), executed via the session
  caller against POST/PUT `/api/inventory`; pantry never touches money, so no
  two-phase. Item removal is deliberately absent (ACL by omission — that's the
  app). System prompt gained the pantry capability; Ask-tab copy updated for
  honesty + a "what do we need?" example chip. **MCP-write tier (Alta's door)
  for the pantry is a later increment, if wanted.** Tests: pantry writes really
  create/flip the item row through the route + log as the person, bad id
  recoverable, write tools appear only with a caller (14 read + 3 write),
  `ledger_inventory` byte-equal to `/api/inventory` through MCP dispatch; four
  loop/route count bumps. Pure clients of gated endpoints → no balance gate;
  suite 346→350 python + 48 render. `ask_smoke.py` migrates to v8, so a live
  pantry question ("add coffee, we're low") is runnable with an
  `ANTHROPIC_API_KEY` (⚠ Pi key expires ~Aug 30). **The pantry MVP (INVENTORY-
  DESIGN steps 1–4) is now CODE-COMPLETE** — tap OR talk. **NOT yet deployed:
  inc 1–4 all await the first pantry deploy, which applies migration `#008
  --live`** (deploy `deploy/deploy.sh origin/main` — Alta runs it). Next after
  deploy: INVENTORY-DESIGN step 5 (purchase-feed auto-population + restock
  prediction), each its own increment.
- **DEPLOYED TO THE PI (Jul 27, 2026).** The deployed line had drifted ~27
  commits behind `rework`; reconciled and shipped the same day. Pushed
  `rework` (`c01c747`); advanced `main` to rework's exact tree via `--no-ff`
  merge `2ba5056` (first parent = old `main`, fast-forward push); ran
  `deploy/deploy.sh origin/main` on the Pi. GATE PASSED — no money moved, only
  the enumerated structural diff (`api_tokens` table + `schema_version` 5→6);
  migration #006 (the only pending one) applied `--live`; service restarted
  clean. Live-verified over the tailnet: the formerly-404 read endpoints
  (`household_snapshot`, `transactions/search`, all `/api/analytics/*`,
  `income/trend`) now 401 (exist, auth-gated), `POST /api/tokens` now 401 (was
  405), bad bearer → 401. The Pi now runs the full read tier + bearer auth.
  Rollback backup: `finance.db.bak-2026-07-27-190635`.
- **MCP server stood up on the Pi over Tailscale (Jul 29, 2026) — Phase 3
  done.** `ledger-mcp.service` installed (sed-rewritten pi→altamash), `.env`
  `LEDGER_MCP_*` set (a per-person `read` token minted through the app's
  login→`POST /api/tokens`, bound to the Pi's tailnet IP `100.108.237.13:8765`,
  tailnet-only, no Funnel). Verified from this Mac over the tailnet: a real
  streamable-HTTP client at `http://raspberrypi:8765/mcp` lists all 13 tools
  and `ledger_household_snapshot` returns live data (full chain proven: token
  valid → Flask reachable → real numbers). Alta's Claude Code `ledger` client
  repointed from the local synthetic soak to the Pi. Alta now soaks the read
  tier against REAL data over Tailscale (the design's intended week-long soak).
- First read-tier brick done: `GET /api/household_snapshot` — one-call
  overview composing `derive_balance`/`spending_summary`/`income_summary` +
  goals + bills, every money field as `{cents, display}` (`money_display`
  helper), no new math (can't disagree with the dashboard). Decision-
  independent: serves both doors, needs no auth/exposure decision yet,
  matches current full-visibility default. Pure read; zero-diff gate; suite
  210→214.
- Second read-tier brick done (Jul 27): `GET /api/transactions/search` —
  the assistant's EVIDENCE tool (the one read it needed and lacked), vs the
  summary endpoints for totals. Optional ANDed filters (query substring,
  date_from/to, direction, income_type, category, paid_by username→owner),
  paginated (limit 1..100/offset, `total_matches`+`has_more`), money
  `{cents, display}`. Pure read; zero-diff gate; suite 223→233. **Read tier
  is now functionally complete** for the assistant (snapshot + summaries +
  trends + search); what remains for step 7 is the door decision, then
  `api_tokens`/auth + the client, then the two-phase write tier.

Also queued (analytics extensions, not blocking step 7): **Tier A backend
is complete (#8–12).** All are pure read-time derivations/endpoints under
`/api/analytics/*`, zero-diff gated, `{cents, display}` money. What remains
on the analytics track:
- Analytics-tab **frontend batch — done (Jul 29, 2026).** The Analytics tab
  now renders all Tier A reads under the existing month-window nav: the
  income-vs-spend chart (unchanged), a rolling savings-rate strip
  (`savingsRateTrendHTML`), spending composition — category mix reusing the
  dashboard `cat-bar` + a top-merchants list (`spendingCompositionHTML`), a
  category-trend drill-in for the month's biggest category, no picker
  (`categoryTrendHTML`), per-member paid/owed/net (`memberBreakdownHTML`),
  and bill planned-vs-actual reusing the badge palette (`billVarianceHTML`).
  All pure helpers in `render.js`, unit-tested in the `node tests/test_render.js`
  seam (26→35 checks); `renderAnalytics` fans out the six endpoints with
  `Promise.all`. Money shapes handled per-endpoint: composition/member/bill
  speak `{cents, display}`, income/category trends speak plain dollars.
  Frontend only — no gate. Visually verified against `style.css` in a
  throwaway harness across a refund month (all-green chart) and negative
  member-net / bill-variance / category-delta (the −$X fix below in action).
- **Tier B (#13–16)** — heuristics (recurring detection, cash-flow
  forecast, anomaly flags, goal pace); budgets (Tier C) stays deferred.
None of this blocks step 7 (the assistant is a sibling surface, not built
on the analytics). The read tier the assistant needs is already complete.

Cosmetic follow-up surfaced by inc 5 — **done (Jul 29, 2026).** A negative
money value (a month's Spent when a refund exceeds spend; a negative member
net, bill under-run, or category MoM delta) now renders `−$353.51`, minus
before the symbol, matching the server's `money_display` and the income
card's existing `−`. The fix was one function: `Render.fmt` in `render.js`
(the backend `money_display` was already correct). Pinned in
`tests/test_render.js`.

Repo housekeeping: `rework` → `main` merge **done (July 26, 2026)** —
`origin/main` now == `rework`'s tree via merge commit `09c8694`
(fast-forward push, see the reconciled topology note above). `v1.0` tagged
at `41c2040` on origin. Remaining non-feature task: Tailscale on the Pi
for phone access (headless).

After each increment, update this "Current position in the sequence"
section to reflect what's done and what's next.

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
