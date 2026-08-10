# Ledger — progress log

The full append-only narrative of the `rework` build: every increment, its
gate result, deploy record, and the design decisions behind it. Split out of
`CLAUDE.md` on Aug 9, 2026 so that file stays the load-bearing rules; the
history before the split lives in git. **Append new increment records here.**

---

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
13. Recurring-charge / subscription detection — **DONE + DEPLOYED (Aug 5,
    2026), backend + MCP.** `recurring_charges(db)` clusters outflows by
    **normalized merchant + IDENTICAL amount** and flags those recurring on a
    **regular cadence** (≥3 charges, gaps within ±40% of the median); reports
    the detected interval, a cadence label (**any rhythm** — weekly..yearly, or
    "every ~N days", Alta's call), and the next expected date. A *suggestion*,
    not an authority — conservative bar + a merchant normalizer (`_normalize_
    merchant`) that biases to **under-merge** so it never invents a phantom
    subscription (verified on real bank descriptions: NETFLIX.COM/Spotify/Citi/
    NJM normalize clean, no false merges). Clock-free like `restock_forecast`;
    outflows-only + settlements excluded → tripwire-covered, no exemption. `GET
    /api/analytics/recurring` (money {cents, display}) + shared read tool
    `ledger_recurring_charges` (both doors — analyst/MCP and Ask; read-tool count
    14→15, MCP tools 18→19). **Backend-first** (Alta's call) — the "Subscriptions"
    card joins the analytics frontend batch later. Zero-diff balance gate PASS
    (no money path) + live deploy GATE PASS (no migration, v9;
    `finance.db.bak-2026-08-05-012130`). Suite 369→**381**. Honest caveat: sparse
    on real data until a few months of history (a monthly charge needs 3 months).
14. Cash-flow forecast — **DONE + DEPLOYED (Aug 5, 2026), backend + MCP.**
    `cash_flow_forecast(db, period)` = `income_summary`'s net-so-far (income −
    spend, month-to-date) minus the bills still UNPAID this period (from
    `bill_variance`) → projected month-end **NET CASH FLOW**. **Grounding
    correction (important):** the roadmap's premise here was wrong — `income_
    rules` do NOT encode cadence, goals have NO scheduled contributions, and the
    app tracks NO cash/account balance. So it's a deliberate **conservative
    FLOOR**: bills-only (no inferred paycheck → real month-end is usually
    better), **net flow not a balance**, goals excluded (Alta's call). EXEMPT in
    the tripwire like `income_summary` (counts income by construction; the bills
    half is guarded by `bill_variance`). `GET /api/analytics/cash-flow-forecast`
    + shared read tool `ledger_cash_flow_forecast` (both doors; analyst granted).
    Zero-diff gate PASS; suite 381→387. (Not `_monthly_series`-based — it's a
    single-period floor, not a trailing window.)
15. Anomaly flags — **DONE + DEPLOYED (Aug 5, 2026), backend + MCP.**
    `anomaly_flags(db, month, threshold_pct=50)`: flags a category whose month
    spend is ≥ threshold% above its trailing 3-month **exclusive** average (vs
    recent norm, not a dampened self-average). Two noise guards: positive
    baseline + a min $20 jump (so a tiny category's big-% wobble doesn't spam).
    Reads `spending_summary` → refund-netted → EXEMPT in the tripwire like
    `category_trend` (a refund can legitimately clear a flag; proven by a test).
    `GET /api/analytics/anomalies` (month + tunable `threshold`) + shared read
    tool `ledger_anomaly_flags` (both doors; analyst granted). Default 50%
    (Alta's call). Zero-diff gate PASS; suite 396→407. **Completes analytics
    Tier B (#13–16 all shipped).**
16. Goal pace / projection — **DONE + DEPLOYED (Aug 5, 2026), backend + MCP.**
    `goal_pace(db, as_of)`: per goal, net saved ÷ days since the first
    contribution (**lifetime-average** rate, Alta's call) → a monthly rate + a
    projected finish date, compared to `target_date` → status
    complete/on_track/behind/projected/no_pace. Signed `amount_cents` so
    withdrawals net out; float-free (`round_ratio`). Reads goals +
    goal_contributions ONLY (never transactions) → trivially tripwire-safe, no
    exemption. `GET /api/analytics/goal-pace` (`as_of` defaults to today) +
    shared read tool `ledger_goal_pace` (both doors; analyst granted). Zero-diff
    gate PASS; suite 387→396. First of the three forecasts with real data to
    chew on (two live goals), so immediately useful.

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
  DESIGN steps 1–4) is now CODE-COMPLETE** — tap OR talk.
- **DEPLOYED TO THE PI (Aug 3, 2026) — pantry MVP is LIVE.** Advanced `main`
  to rework's tree via `--no-ff` merge `0eb0301` (first parent = old main
  `26030f0`, tree byte-identical to rework, fast-forward push). Note: the FIRST
  `deploy.sh origin/main` run re-deployed old code — that run's internal
  `git fetch` landed a beat before `0eb0301` propagated, so it gated a no-op
  against unchanged code (safe: backup only, GATE PASS zero-diff, but
  `/api/inventory` still 404 over the tailnet). A manual `git fetch origin`
  (origin/main `26030f0`→`0eb0301`) then a re-run did the real thing:
  **GATE PASS** with the enumerated `#008` diff only (`items` None→0 +
  `schema_version` 7→8, **balance and every monthly total unchanged to the
  cent** — inventory never touches money), migration `#008` applied `--live`,
  `pifinance` restarted clean. Verified over the tailnet: `/api/inventory` now
  401 (was 404), served `render.js`/`app.js` carry the pantry frontend.
  Rollback backup `finance.db.bak-2026-08-03-211145`. **CONFIRMED LIVE on
  Charlee's/Alta's phone** — the Pantry tab and add both work (after a hard
  refresh). Lesson (2nd time, after the mobile-nav fix): a frontend deploy needs
  a per-device hard refresh to bust cached `app.js`/`render.js` — a stale cache
  made pantry "add" appear dead until refresh. `ledger-mcp` gains
  `ledger_inventory`; restart it + reconnect the client to see the new tool
  (app pantry works regardless).
- **Cache-busting shipped (Aug 3, 2026) — so future frontend deploys self-bust.**
  `index()` (app.py) now stamps `style.css`/`render.js`/`app.js` with a
  `?v=<mtime>` token and serves the shell `Cache-Control: no-cache`. Per-file
  mtime read at serve time (no build step, no git at runtime): a deploy's
  `git checkout` advances only the changed files' mtime, so the browser
  re-fetches exactly those. Route only → no balance gate;
  `test_index_cache_busting` locks it (stamped, no-cache, tracks mtime, stamped
  URL still serves). Suite 350→353 + 48 render. **One more hard refresh needed
  after THIS deploys** (to pick up the no-cache shell + new app.js); every
  frontend deploy after is automatic.
- **Cache-busting DEPLOYED (Aug 3, 2026).** Advanced `main` via `--no-ff` merge
  `be27345` (first parent = old main `0eb0301`, fast-forward push); a clean
  `git fetch origin && deploy.sh origin/main` → **GATE PASS zero-diff, no
  migration**, service restarted. Verified over the tailnet: `/` serves the
  `?v=<mtime>`-stamped tags + `Cache-Control: no-cache`, stamped asset 200.
  Both phones hard-refreshed once (the LAST manual refresh) → pantry add works.
  Future frontend deploys now self-bust.

**INVENTORY-DESIGN step 5 — purchase-feed auto-population, underway (Aug 3, 2026).**
Scoped with Alta: matching = **auto-guess by item name + optional override
phrase**; scope = **restock hints only** (new-staple suggestions deferred). Key
honest constraint surfaced first: SimpleFIN gives the MERCHANT, not products, so
this works for merchant-identifiable staples (dog food ← "chewy") and can't tell
coffee from milk inside a grocery run — the design leans on that. Model mirrors
income_rules; "suggest, don't assert."
- **Inc 5a done (Aug 3, 2026).** Migration #009 — `items.restock_match` (nullable
  override phrase; NULL → derivation falls back to the item name). Guarded `.py`
  (PRAGMA gate), `REQUIRED_SCHEMA_VERSION` 8→9. **Enumerated-diff gate PASS**
  (notes/009: only `schema_version` 8→9 — adding a column changes no row count;
  inventory never touches money). Suite 353.
- **Inc 5b done (Aug 3, 2026).** The logic. `restock_suggestions(db)` — each
  staple low/out AND with a matching **outflow** since it ran low (purchase dated
  ≥ the item's `updated_at`), matched by `restock_match` phrase or else the item
  name (case-insensitive, `instr`); one per staple, most-urgent first, evidence =
  the most recent matching purchase. OUTFLOWS ONLY → tripwire-covered; a
  manufactured matching inflow is proven ignored. `set_item_match` verb
  (set/clear, audited, registered in CORE-DESIGN); `add_item` accepts
  `restock_match`; PUT `/api/inventory/<id>` now takes status and/or
  restock_match; GET `/api/inventory` adds `restock_suggestions` (purchase amount
  {cents, display}). `item_to_json` carries `restock_match`. `ledger_inventory`'s
  shared description mentions suggestions → **both doors already surface them**
  (Charlee/Alta can ask "what did I probably restock?" NOW). No schema change →
  **zero-diff balance gate PASS**; suite 353→362.
- **Inc 5c done (Aug 4, 2026) — the Pantry restock UI.** `inventoryHTML` gained a
  "Looks like you restocked?" nudge card at the top of the pantry view (only when
  `restock_suggestions` is non-empty): each row shows the evidence purchase
  (`Bought <desc> · <short date> · <display amount>`) and a one-tap "Yes,
  restocked" → `data-restock-confirm` → the existing `set_item_status(id,
  'stocked')` (drops it off the shopping list). Each staple row also gets a faint
  🔎 match-editor (`data-item-match`) — a `prompt()` pre-filled from
  `window._inv` (stashed in `renderInventory`, the `_txns`/`_bills` pattern; no
  user content in an attribute since `esc` doesn't escape quotes) that PUTs
  `restock_match` (blank clears); a set phrase shows inline as `🔎 matches "…"`.
  New `shortDate` helper (exported). Three small CSS rules (`.restock-card` green
  left-accent, `.item-match:hover` neutral not danger-red, italic `.match-hint`).
  Frontend only — no schema/derivation/route change, **no balance gate**; render
  seam 48→52, full python suite 362 green. Visual sign-off was the render-seam +
  a real-markup dump (the in-app Browser tool was wedged again — 300s navigate
  timeouts, same as recent sessions); live on-device check comes after deploy.
  **DEPLOYED TO THE PI (Aug 4, 2026).** Advanced `main` to rework's tree via
  `--no-ff` merge `3cf9784` (first parent = prior main `a07f470`, fast-forward
  push; rework committed at `e2ae7aa`); `deploy/deploy.sh origin/main` → **GATE
  PASS, zero diff, no structural changes** (frontend only; migrate a no-op —
  already schema v9). Rollback backup `finance.db.bak-2026-08-04-161552`. **This
  deploy was the first to actually exercise the #009-shipped `deploy.sh`
  hardening** (both self-replaced before running last time): the backup ran
  through the WAL-safe `VACUUM INTO` path, and `deploy.sh` auto-restarted
  `ledger-mcp` (`Requires=` stops it with the app) — no manual restart needed.
  Both ops gaps [[ledger-ops-layer]] flagged are now closed and proven. `/api/
  status` 200; on-device check of the restock nudge + 🔎 match editor is a
  per-device hard-refresh away.
- **Step 5 SECOND HALF — restock *prediction* from cadence — DONE + DEPLOYED
  (Aug 5, 2026).** New read-time derivation `restock_forecast(db, min_purchases=3)`:
  for each staple with **≥3 matching purchases on distinct days**, the **median
  gap** between purchase dates projects `predicted_date = last_purchase +
  interval` (median is robust to one odd early/late buy; ≥3 = the conservative
  "suggest don't assert" bar, Alta's call). Purchase-matching factored out of
  `restock_suggestions` into a shared `_matching_purchases` helper (behavior-
  neutral — its tests stayed green). **Deliberately clock-free**: the derivation
  returns only history-derived facts (interval/last/predicted), never a "today",
  so it stays an inflow-insensitive aggregate the tripwire covers **with no
  exemption**; the "days-until / overdue" framing is computed at the **view
  layer** against the client's real date. (Honest deviation from the design doc's
  "rides `_monthly_series`" sketch — cadence is intervals between purchases, not
  monthly buckets.) `/api/inventory` gains `restock_forecast` (rides
  `ledger_inventory` to both doors, pure passthrough — MCP byte-equality test
  green). Frontend: a honey-accented **"Coming up"** pantry card
  (`restockForecastHTML`, stocked staples due ≤14d or overdue, soonest-first,
  informational — no auto-add). Scope was **prediction only** (Alta's call);
  **new-staple suggestions deferred** to their own increment. Seeded **zero-diff
  balance gate PASS** (23 values, no money path touched) + live deploy GATE PASS
  (no migration, schema stays v9; `finance.db.bak-2026-08-05-001709`); `ledger-mcp`
  auto-restarted. Suite 362→**369**, render seam 52→**57**. Honest caveat: sparse
  on real data at first (bank feed only ~2 weeks old → few staples have 3+
  matching purchases yet; fills in as history accrues), and inherits 5b's
  merchant-not-product limitation.
- **DEPLOYED TO THE PI (Aug 4, 2026) — step-5 backend (5a `#009` + 5b) is LIVE.**
  Advanced `main` to rework's tree via `--no-ff` merge `a07f470` (first parent =
  old main `be27345`, tree byte-identical to rework, fast-forward push); ran
  `deploy/deploy.sh origin/main` on the Pi (altamash). **GATE PASS** — balance and
  every monthly total unchanged to the cent, only the enumerated `#009` structural
  diff (`schema_version` 8→9; inventory never touches money); migration `#009`
  applied `--live`, `pifinance` restarted clean, `/api/status` OK; `ledger-mcp`
  restarted by hand (this deploy's `deploy.sh` doesn't yet auto-restart it — that
  fix shipped now, takes effect next deploy). Rollback backup
  `finance.db.bak-2026-08-04-140904`. Merge carried the whole Aug-4 agent/ops
  layer alongside #009 (a deliberate batch — safe because the gate is
  authoritative and every non-#009 change is tooling/docs/deploy/frontend, no
  schema/money-derivation impact). Gated via the `ledger-release` agent; the pre-
  gate step-C needed the expectation file pulled from `origin/rework`
  (`git show origin/rework:notes/009-gate-expectation.seed.json`) since the
  working tree was still at pre-#009 `be27345`. The chat-door restock suggestions
  are now live (Charlee/Alta can ask "what did I probably restock?").
- Alternative tracks if step 5 is paused: analytics Tier B (#13–16) or the
  income-visibility policy.
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

**Agent + ops layer built (Aug 4, 2026) — a hands-off operations tier around
the app, all on `rework` and pushed; frontend/tooling only (no app/schema/
derivation change), so nothing to gate.**
- Seven role-scoped subagents in `.claude/agents/` under one standard,
  `docs/OPERATING-CHARTER.md`: `ledger-analyst` (read-only analysis over the MCP
  reads), `ledger-security` (defensive audit, advisory), `ledger-maintenance`
  (deps/back-end, stops before commit), `ledger-ops` (live-Pi SRE, recommend-
  only), `ledger-health-sweep` (weekly code+dep sweep), `ledger-release`
  (gated-deploy copilot — see NEXT), `ledger-chief-of-staff` (reconciles reports
  → one weekly briefing). Cross-session detail: memory [[ledger-ops-layer]].
- **Pi Ops guardian** — `deploy/ops-health-check.sh` + `deploy/pifinance-ops.
  {service,timer}`, installed OUT of the repo at `~/pifinance-ops/` on the Pi
  (so `deploy.sh`'s clean-tree guard stays happy), daily 07:00, LIVE + green.
  Checks service/disk/sync-freshness/backup-restorability/SECRET_KEY/API-key;
  alerts file a GitHub `ops-alert` issue on amber/red via curl+PAT (`.env`:
  `OPS_ALERT_GH_REPO` + `OPS_ALERT_GH_TOKEN`).
- Two cloud routines (RemoteTrigger): Mon 08:00 ET health-sweep, Fri 08:00 ET
  Chief-of-Staff briefing (both file issues on `evenkeel`).
- **Security audit** (`docs/SECURITY-AUDIT-2026-08-04.md`): no exploitable
  vulns; findings are hardening (rate-limit `/api/ask`, cookie Secure flag,
  login timing) + a clean dependency scan. Finding #1 (SECRET_KEY + 2 workers)
  VERIFIED RESOLVED (it's set on the Pi).
- `deploy.sh` hardened: WAL-safe backup (`VACUUM INTO`, not a raw `cp` — a cp of
  the WAL-mode live DB dropped uncheckpointed transactions) and it now restarts
  `ledger-mcp` after a deploy. Both activate on the deploy AFTER the one that
  ships them (deploy.sh backs up + self-replaces before those lines run).
- **Backup retention — self-pruning (Aug 6, 2026) — DEPLOYED.** Third piece of
  the deploy.sh hardening lineage. Problem: deploy.sh writes a `finance.db.bak-*`
  on EVERY run — including gate-failed and same-day propagation-race re-runs —
  with no pruning, so backups accumulated until the Pi Ops guardian's backup-count
  alert fired (28 files on Aug 6, amber two days running: `evenkeel` issues #4, #6;
  hand-pruned to 10 that day). Fix (design in `docs/BACKUP-RETENTION-DESIGN.md`):
  a best-effort prune step (3b) right after the new backup is written + verified —
  keep newest N (`DEPLOY_KEEP_BACKUPS` from `.env`, default 10; keep it < the
  guardian's `MAX_BACKUPS`=12), delete the rest, so the very runs that cause the
  bloat self-limit. **Keep-newest-N** (not time-thinning) — filenames embed the
  timestamp, so lexical sort == chronological. Safety: the just-written `$BACKUP`
  is newest → this run's own rollback point can never be pruned; the off-Pi golden
  backup is outside the glob; nothing is deleted until the new backup passes
  `PRAGMA integrity_check` (immutable=1 — the guardian's integrity-before-trust
  discipline); non-fatal under `set -euo pipefail` so housekeeping never aborts a
  deploy. Prune lives in **deploy.sh, not the guardian** — the guardian stays
  read-only, keeping its `> MAX_BACKUPS` amber as an independent backstop. Verified
  off-Pi by extracting the real `prune_backups` function and sourcing it against
  dummy dirs (11 checks: no-op under limit, keeps exactly N, keeps `$BACKUP`, never
  a sidecar, refuses+warns on a corrupt new backup, honors the `.env` override).
  That dry-run caught a real pre-Pi regression: the optional-key `grep .env`
  aborted the whole deploy under `set -e`+`pipefail` when `.env` lacked the key
  (the default) — fixed with `|| true`. Ops tooling only — no
  schema/verb/derivation/money path, balance gate N/A. Committed on `rework`
  `6b95541`, deployed via `main` `e1fe6b4` (`--no-ff` merge, first parent = prior
  main `3fc0201`, tree == rework; fast-forward push) — **GATE PASS zero-diff, no
  migration**. Shipped with a two-run deploy (the "takes effect on the deploy AFTER
  the one that ships it" quirk): run 1 swapped in the new script, run 2 executed it
  and **pruned 13→10 live**, integrity-gated on the fresh backup. Count on the Pi
  is now 10; guardian reads green on the backup-count line.

**Pantry #009 DEPLOYED (Aug 4, 2026) — done.** The gated migration shipped via
the `ledger-release` agent's go/no-go: `origin/main` advanced to rework's tree
(`--no-ff` merge `a07f470`, first parent = old main `be27345`, fast-forward
push), `deploy/deploy.sh origin/main` on the Pi → **GATE PASS** (enumerated
`schema_version` 8→9 only, no money moved), `#009` applied `--live`, `pifinance`
+ `ledger-mcp` restarted, `/api/status` 200. Rollback backup
`finance.db.bak-2026-08-04-140904`. Deployed line is now schema v9, `origin/main`
tree == `rework`. (Detail in INVENTORY-DESIGN step-5 block above.)

**Pantry step 5 COMPLETE + DEPLOYED (through Aug 5, 2026).** The whole
INVENTORY-DESIGN step 5 now ships: purchase-feed restock *hints* (5a `#009` + 5b
logic + 5c UI) AND restock *prediction* from cadence (`restock_forecast` + the
"Coming up" card). Deployed line is `origin/main` `f8ea301`, schema v9, tree ==
`rework` (`f57cc4a`, one doc-only commit ahead: this CLAUDE.md update). No open
deploy.

**Analytics frontend batch — DONE + merged to `main` (`5ded8a8`, Aug 5, 2026).**
The backend-only Tier B reads now render as Analytics-tab cards: cash-flow
forecast (#14), anomaly flags (#15), recurring/subscriptions (#13), goal pace
(#16) — four pure `render.js` helpers + a `renderAnalytics` `Promise.all`
fan-out, reusing the Garden card classes. Node-seam tested (57→62), frontend
only, no gate. (Deploy to the Pi is Alta's manual `deploy.sh` trigger — verify
before assuming it's live.)

**New-staple suggestions — DONE (Aug 5, 2026; on `claude/ledger-next-increment-prw28h`,
draft PR #5).** The remaining INVENTORY-DESIGN step-5 sibling. New read-time
derivation `new_staple_suggestions(db, min_purchases=3)`: clusters **outflows**
by normalized merchant (reuses `_normalize_merchant`) and offers those bought on
**≥3 distinct days** that aren't already tracked and aren't fixed-amount
subscriptions (excluded via `recurring_charges`) — the *discovery* counterpart
to `restock_suggestions`/`restock_forecast`, which act on already-tracked
staples. **Clock-free + outflows-only** → tripwire-covered, no exemption; reads
transactions + items, never touches money. `GET /api/inventory` gains
`new_staple_suggestions` (`total_spent` `{cents, display}`); rides
`ledger_inventory` to both doors (MCP byte-equality test extended). Pantry UI: a
muted "Bought a lot — track it?" card (`newStapleSuggestionsHTML`); one-tap
Track `add_item`s the merchant as a staple, seeding `restock_match` from the
suggestion. No schema/migration, no money-path change → **zero-diff balance gate
PASS** (`origin/main`→HEAD, 21 values); suite 414 python + 65 render. Honest
caveat inherited from step 5: merchant-not-product (finds a pet store, not one
grocery item). **Not yet deployed** — frontend + read-endpoint, ships through
the zero-gate frontend deploy path when Alta merges + runs `deploy.sh`;
`ledger-mcp` picks up the extended `ledger_inventory` desc on its restart.

**Income-visibility policy — RATIFIED: full transparency (Aug 5, 2026).** The
last open step-7 design question, closed. Design pass held (recorded in
INCOME-DESIGN "Two people can see each other's paychecks" + AGENT-DESIGN "What
the agent layer does to the income visibility question"): both members see all
income, matching the rest of the pooled-visibility app — Alta's call, neither
wants income private. A decisive finding drove it past a mere default: **for a
two-person household, owner-only rows + shared aggregates does NOT give privacy**
— a partner who sees any blended aggregate (net_cash_flow, savings_rate), knows
their own income, and sees shared spend can solve for the other's income by
subtraction. So the only coherent options were full transparency or a real
"personal income mode" (owner-scoped rows *and* aggregates); the household chose
transparency. The policy is the **absence** of per-owner filtering, pinned as a
cross-door tested contract in `tests/test_income_visibility_policy.py` (a
paycheck owned by each member, viewed as the other through BOTH the session and a
read token, shows both incomes + the household total; three teeth proven to bite
by temporarily injecting `WHERE paid_by=<viewer>`). Enforcement point is ready if
ever reopened: `g.auth["user_id"]` is the uniform key (`_resolve_auth` populates
it for session and token alike). **Docs + one load-bearing test only — no schema,
verb, derivation, route, or money-path change → no balance gate applies**; suite
414→**417** python + 65 render. **CORE-DESIGN step 7 (the assistant) is now fully
settled — no open design questions remain.**

Shipped alongside (Aug 5, 2026), via PR #5 (merged to `main` `14901b7`, DEPLOYED
— GATE PASS zero-diff, no migration, backup `finance.db.bak-2026-08-05-130114`):
`new_staple_suggestions` (pantry "Bought a lot — track it?") AND the
**maintenance pin** `mcp>=1.2,<2` (`a9b7dd1`) — mcp 2.0 removed
`mcp.server.fastmcp.FastMCP`, breaking a fresh install of `ledger_mcp.py`; the Pi
was unaffected (its 1.x already satisfied `>=1.2`) but the pin protects rebuilds /
`pip -U` / CI. `rework` reconciled to `main`'s tree afterward (merge `7f03984`,
first parent = old rework tip so the push fast-forwarded; tree byte-identical to
`main` — invariant restored).

**Pantry predicted-low nudge — DONE + DEPLOYED (Aug 5, 2026).** The
INVENTORY-DESIGN step-5 payoff: `restock_forecast`'s "Coming up" card was
read-only, so the prediction never *did* anything. Now an **overdue or due-today**
stocked staple (predicted_date ≤ the client's today, computed at the view layer —
the derivation stays clock-free) gets a one-tap **"Mark low"** button that flips it
to `low` via the existing `set_item_status` verb/endpoint, dropping it into "Need
to buy." Future "heads-up" rows keep their date badge, no action — the nudge only
fires when the prediction says you're probably low *now*. Still a human-confirmed
suggestion, never an auto-flip (INVENTORY-DESIGN discipline). **Frontend only** —
`restockForecastHTML` (render.js) grows the button on `days <= 0` rows, one line
of `data-mark-low` wiring in app.js reusing `setItemStatus`; no schema, verb,
derivation, route, or money path → **no balance gate**. render seam 65→66 (overdue
& due-today actionable, future/1-day-out not); full python suite 417 green. Visual
pass in light AND dark via a throwaway harness rendering the real function against
`style.css` (in-app Browser tool worked this session). Deployed via `main`
`47fb5b0` (`--no-ff` merge, first parent = prior main `14901b7`, tree == rework),
`deploy.sh origin/main` → GATE PASS zero-diff, no migration; verified live
(`data-mark-low` served from the Pi's `render.js`, `/api/status` 200).

**Pantry broken-match detector — DONE, NOT YET DEPLOYED (Aug 5, 2026).** The
brainstorm's highest-leverage inference: the match phrase (`restock_match`, or the
item name when unset) is the linchpin of EVERY purchase inference — restock hints,
forecast, and the predicted-low nudge all bottom out in `_matching_purchases` — so
a staple with a wrong/too-specific phrase silently gets no hints forever and
nobody would know. New read-time derivation `unmatched_staples(db)`: active
staples whose phrase has matched ZERO purchases ever; each carries name,
`restock_match`, `matched_by` (phrase|name), and `tracked_since` (created_at).
**Clock-free** (a "tracked ≥21 days" grace lives at the view layer against the
client date, so a just-added staple isn't nagged) and **outflows-only** via
`_matching_purchases` (an inflow naming the item never counts as a match, so a
broken staple stays surfaced) → tripwire-covered, no exemption; reads items +
transactions, never touches money. `GET /api/inventory` gains `unmatched_staples`;
rides `ledger_inventory` to both doors (MCP byte-equality test extended). Pantry
UI: a muted "Check the match?" card (`unmatchedStaplesHTML`) listing each
long-unmatched staple with a **"Fix match"** action reusing the existing
`data-item-match` editor (no new app.js wiring). A REVIEW prompt, never an
assertion the phrase is wrong — some staples are bought inside grocery runs and
can't match by product (the step-5 merchant-not-product limit). No schema/
migration, no money path → **zero-diff balance gate PASS** (`origin/main`→HEAD);
suite 417→**422** python + 66→**70** render. Visual pass in light+dark via harness.
**DEPLOYED (Aug 5, 2026)** via `main` `16d575d` (`--no-ff` merge, first parent =
prior main `47fb5b0`, tree == rework), `deploy.sh origin/main` → GATE PASS
zero-diff, no migration; `ledger-mcp` restarted (picks up the extended
`ledger_inventory` passthrough). Verified live: the Pi's `render.js` serves
`unmatchedStaplesHTML` + "Check the match?". Deploy note: the first `deploy.sh`
run hit the propagation race (its `git fetch` got the prior `47fb5b0` a beat
before `16d575d` landed, so it re-shipped predicted-low); a second `git fetch
origin && deploy.sh origin/main` deployed `16d575d` — the same race+fix seen on
the Aug 3 pantry MVP deploy.

**Pantry list-rot detector — DONE, NOT YET DEPLOYED (Aug 5, 2026).** Third pantry
inference this session, and the exact inverse of `restock_suggestions`. New
read-time derivation `stale_shopping_items(db)`: staples that have been **low or
out** for a while with **no matching purchase since they ran low** — sitting on
the shopping list, forgotten (restock_suggestions owns the ones WITH a purchase-
since, a probable restock; this owns the ones without). Each carries name,
status, and `low_since` (updated_at, the day it went low/out). **Clock-free** (a
"low/out ≥14 days" grace lives at the view layer) and **outflows-only** via
`_matching_purchases` (an inflow naming the item is never the missing restock, so
a neglected item stays surfaced) → tripwire-covered, no exemption; reads items +
transactions, never touches money. `GET /api/inventory` gains
`stale_shopping_items`; rides `ledger_inventory` to both doors (MCP byte-equality
test extended). Pantry UI: a "Still need these?" card (`staleShoppingHTML`) below
"Need to buy", each row showing how long it's been neglected + a **"Not anymore"**
action reusing the existing `data-item-remove` archive (no new app.js wiring;
still-need-it → leave it, it stays on the list). No schema/migration, no money
path → **zero-diff balance gate PASS**; suite 422→**426** python + 70→**73**
render. Visual pass light+dark via harness. **DEPLOYED (Aug 5, 2026)** via `main`
`ed38edd` (`--no-ff` merge, first parent = prior main `16d575d`, tree == rework),
`deploy.sh origin/main` → GATE PASS zero-diff, no migration (no propagation race
this time); verified live (`staleShoppingHTML` + "Still need these?" served,
prior cards intact).

**Pantry money tie-in — DONE, NOT YET DEPLOYED (Aug 5, 2026).** INVENTORY-DESIGN
step 5's explicitly-named deferred future ("$40/mo on coffee"). New read-time
derivation `staple_spend(db, min_purchases=3)`: for each tracked staple with ≥3
matching purchases on distinct days, `total_cents` + `monthly_cents` (total ÷
inclusive calendar months first→last, float-free via `round_ratio`) — what the
habit costs, over the SAME `_matching_purchases` every restock inference uses.
**Clock-free** (total + span are history-derived, no "today") and **outflows-only**
(an inflow never inflates a staple's cost) → tripwire-covered, no exemption; it
REPORTS money but never MOVES it (integer cents, computed on read — the money
invariants hold). `GET /api/inventory` gains `staple_spend` with `total`/`monthly`
as `{cents, display}` at the edge; rides `ledger_inventory` to both doors (MCP
byte-equality test extended). Pantry UI: a "What your staples cost" card
(`stapleSpendHTML`, priciest-first) below Staples — "Coffee · $60 over 3 mo · 3×"
with a `~$20.00/mo` badge; informational, no action, no client date needed.
Honest limit (step 5): merchant-level — the whole coffee shop, not one cup; a
grocery-hidden staple shows nothing. No schema/migration; reports money but the
gate's balance/monthly/rowcount snapshot is untouched → **zero-diff balance gate
PASS** (`origin/main`→HEAD, 23 values); suite 426→**430** python + 73→**75**
render. Visual pass light+dark via harness. **DEPLOYED (Aug 5, 2026)** via `main`
`a63f6d1` (`--no-ff` merge, first parent = prior main `ed38edd`, tree == rework),
`deploy.sh origin/main` → GATE PASS zero-diff, no migration; verified live
(`stapleSpendHTML` + "What your staples cost" served, prior cards intact).

**Pantry post-shopping review nudge — DONE, NOT YET DEPLOYED (Aug 5, 2026).** The
last of the four brainstormed pantry inferences, and the one that leans hardest
INTO the merchant-not-product limit: the feed can't say WHAT you bought inside a
grocery run, so instead of guessing which staples you restocked, it prompts you
to review. New read-time derivation `last_shopping_trip(db)`: the most recent
outflow in a shopping category (`SHOPPING_CATEGORIES = Groceries, Household`),
`{date, merchant, category}` or None. **Clock-free** (returns only the trip date)
and **outflows-only**, settlements excluded (like `top_merchants`) → an inflow
never counts as a trip, tripwire-covered, no exemption; reads transactions, never
touches money. `GET /api/inventory` gains `last_shopping_trip`; rides
`ledger_inventory` to both doors (MCP key-set test extended). Pantry UI: a gentle
green nudge (`postShoppingHTML`) above "Need to buy" — "You shopped yesterday · 🛒
FRESH MART — check off anything you restocked below" — shown ONLY when the trip is
within a **3-day window** (view-layer) AND the shopping list is non-empty, so it
self-clears once the list is emptied or the trip ages out. No action of its own
(it points at the Need-to-buy Got-it buttons — no duplication). No schema/
migration, no money path → **zero-diff balance gate PASS**; suite 430→**433**
python + 75→**78** render. Visual pass light+dark via harness. **DEPLOYED (Aug 5,
2026)** via `main` `e5b50bb` (`--no-ff` merge, first parent = prior main
`a63f6d1`, tree == rework), `deploy.sh origin/main` → GATE PASS zero-diff, no
migration; verified live (`postShoppingHTML` + "check off anything you restocked"
served, prior cards intact).

The pantry inference track is complete (predicted-low ✅ + broken-match ✅ +
list-rot ✅ + money-tie-in ✅ + post-shopping ✅, all deployed).

**ANALYTICS TIER C — BUDGETS, underway (Aug 5, 2026).** Category spending limits,
designed in `docs/BUDGETS-DESIGN.md` (governs, checked against CORE-DESIGN).
Settled with Alta: **simple monthly budgets** (fresh limit each month, NOT
rollover envelopes), an **Analytics-tab card** (not a dedicated view), per-category
limits, any category budgetable (picker defaults to `DEFAULT_CATEGORIES`),
display-only (no alerts v1), show an "unbudgeted spend" line, refunds net against
actuals via `spending_summary`. Build order (one migration/verb per merge): **#010
migration → `set_budget`/`remove_budget` verbs → `budget_status` derivation +
endpoint → Analytics UI.** First schema-touching feature since #009 — inc 1
carries an enumerated-diff gate + a `--live` migration on deploy; the rest are
zero-gate reads/frontend.
- **Inc 1 done (Aug 5, 2026).** Migration #010 creates the empty `budgets` table
  (schema_version 9→10): `category UNIQUE` + `amount_cents` (monthly limit) +
  soft-delete, mirroring #008's additive posture. `budgets` into `GOVERNED_TABLES`;
  `set_budget`/`remove_budget` rows added to CORE-DESIGN's registry first (verbs in
  inc 2); `REQUIRED_SCHEMA_VERSION` 9→10. **Enumerated-diff gate PASS** (notes/010:
  `budgets`=0 + `schema_version` bump, nothing else — a budget is not a
  transaction, never touches money); suite 433 green. NOT deployed (deploy comes
  with a later inc, `#010 --live`).

- **Inc 2 done (Aug 5, 2026).** Verbs `set_budget`/`remove_budget` in `actions.py`
  (validate → edit → audit, the standard contract): `set_budget` upserts one row
  per category (`INSERT … ON CONFLICT(category) DO UPDATE`, reactivating a removed
  one), `remove_budget` soft-deletes (`active=0`, NotFound on missing/already-
  inactive). Thin routes `GET`/`POST /api/budgets` + `DELETE /api/budgets/<id>`
  (`login_required`, write scope for bearer), money `{cents, display}` at the edge
  via `budget_to_json`. `test_budget_verbs` (5) + `test_budget_routes` (5, incl.
  bearer write-scope gating). No new schema (rides inc 1's v10), no money path →
  **isolated zero-diff gate PASS** (`bc8fddb`→HEAD); suite 433→**443**. Still not
  deployed (deploy with a later inc, `#010 --live`).

- **Inc 3 done (Aug 5, 2026).** Derivation `budget_status(db, period)`: for each
  ACTIVE budget, `budgeted`/`actual`/`remaining`/`over`/`pct` where **actual is
  refund-netted** (reads `spending_summary`), plus `unbudgeted_spend_cents` (net
  spend in categories with no budget, so nothing hides); over-budget sorts first.
  EXEMPT in the tripwire like `category_trend` (reads refund inflows via
  spending_summary — bounded exemption; also takes a `period` arg). `GET
  /api/analytics/budget-status` (period default `current_period()`, money `{cents,
  display}` at the edge, `pct`/`over` pass through). `test_budget_status` (4:
  refund netting, over-budget, unbudgeted, over-first sort) + `test_budget_status_
  route` (2). No new schema, no money-path change → **isolated zero-diff gate PASS**
  (`e085bf2`→HEAD); suite 443→**449**. Still not deployed (deploy with inc 4,
  `#010 --live`).

- **Inc 4 done (Aug 5, 2026) — feature CODE-COMPLETE.** The Analytics-tab Budgets
  card: `budgetStatusHTML(data, categories)` (render.js) — each budget a progress
  bar (green under, red over via `.cat-bar i.over`, capped at 100% width) with
  "actual of limit · pct%" and a remaining/over badge, ✎ edit (prompt) + ✕ remove;
  an "Unbudgeted spend" line so nothing hides; a set-a-budget form (category
  datalist from `/api/categories` + amount). `renderAnalytics` fans in
  `/api/analytics/budget-status` + `/api/budgets` (ids) + `/api/categories`,
  stashes `window._budgetStatus`/`window._budgets` (index-addressed actions, no
  user content in attributes); `setBudget`/`editBudget`/`removeBudget` in app.js
  wired in `wireMain`. One CSS gotcha fixed: the bar is stacked (not in a flex
  row), so `.budget-bar` gives it explicit block/full width. Frontend only → no
  gate; render seam 78→**80**, python suite 449. Visual pass light+dark via
  harness (caught the collapsed-bar bug).

**BUDGETS FEATURE IS CODE-COMPLETE (inc 1–4).** Ready for ONE batched deploy that
ships `#010 --live` + the verbs + derivation + UI together: advance `main` to
rework, `deploy.sh origin/main` runs its dry-run gate (PASSES — money-neutral —
and prints the `#010` structural diff: `budgets` None→0 + `schema_version` 9→10,
eyeball against notes/010), applies `#010 --live`, restarts. First `--live`
migration since #009. Alternative if paused: nothing else substantial is open.

**Mobile UI fixes — DONE, NOT YET DEPLOYED (Aug 5, 2026)** (reported by Alta on an
iPhone 14 Pro; frontend-only, no gate). Three fixes, all in `static/`:
- **Ask tab zoomed in on open.** `.ask-bar input` was `font-size:15px`; iOS Safari
  auto-zooms a focused input under 16px, and the Ask tab auto-focuses its input on
  open (app.js `$("#ask-input").focus()`), so opening the tab zoomed the page.
  Bumped to `16px` (the iOS threshold) — the standard fix.
- **Add-expense calendar popped on open.** `openTxnDialog` did `showModal()`, which
  auto-focuses the first field (the date input), and iOS shows its date picker on
  focus — so tapping the ⊕ FAB opened the calendar immediately. Now it blurs the
  auto-focused element after `showModal()`; the user opens the picker by tapping
  the date field.
- **Date/amount overlap in the add dialog.** `.row2` is a `1fr 1fr` grid; a native
  date input's min-content (calendar icon + spinners) is wide and, with grid
  items' default `min-width:auto`, overflowed its column into the amount field on
  a phone. Added `.row2 > label, .row2 input { min-width: 0 }` so they shrink.
  All three visually verified at 375px in light+dark harnesses. render seam 80,
  suite 449. (Same picker-on-open pattern may affect other date-first dialogs
  (bill/pay/contrib) — not reported, the same one-line blur fixes them if wanted.)

**Declarative action parameters — SCOPED, not started (Aug 5, 2026).** Ontology
convergence #2, designed in `docs/ACTION-SCHEMA-DESIGN.md`. The problem: a write
action's parameter contract is hand-maintained in 3–4 places (e.g. `classify_
inflow`'s income-type vocabulary lives in `INCOME_TYPES` frozenset AND
`agent_write_tools`' `REAL_INCOME_TYPES` enum AND `ledger_mcp`'s Field-description
prose), so adding an item status / income type silently leaves the agent tools'
enums stale — nothing binds them. Design (decided with Alta): **full spec, write
verbs only** — a `PARAM_SPECS` registry in `actions.py` (enums *reference* the
existing constants, no copies) that GENERATES the Ask + MCP write-tool schemas,
extending the same shared-source pattern that already binds `agent_read_tools.
DESCRIPTIONS` across both doors. Structural shape only; semantic validation stays
in the verb. Consolidation, not new capability → **no schema/migration/gate**.
Build order: (1) `PARAM_SPECS` + byte-equal test vs today's schemas; (2)
`agent_write_tools` generates from it; (3) `ledger_mcp` consumes it + drift test
extended; (4–5 optional) verb-side first-pass validation (must preserve the
parity-pinned error strings) + generated CORE-DESIGN registry table.
- **Inc 1 done (Aug 5, 2026).** `PARAM_SPECS` + `param_schema(verb)` +
  `Param` dataclass in `actions.py` — the single source for the three agent-exposed
  write verbs' parameter schemas (`classify_inflow`, `add_item`, `set_item_status`).
  Enums reference NEW ordered vocabulary tuples (`REAL_INCOME_TYPE_ORDER`,
  `ITEM_KIND_ORDER`, `ITEM_STATUS_ORDER`) that the membership frozensets now
  DERIVE from (`INCOME_TYPES`/`ITEM_KINDS`/`ITEM_STATUSES` = `frozenset(order)`),
  so offered-choices ↔ validated-vocabulary can't drift, and the order (a
  presentation choice) stays human-controlled. **Nothing consumes PARAM_SPECS yet**
  (inc 2/3 do) → zero behavior change; the constant refactor is value-identical
  (full suite proves it). `tests/test_action_schema.py` (4): `param_schema(verb)`
  **byte-equal** to today's hand-written `agent_write_tools` `input_schema` (tooth
  verified — perturbing the hand-written schema fails it), enum-order ↔ frozenset
  membership bound, `REAL_INCOME_TYPE_ORDER` ↔ `agent_write_tools.REAL_INCOME_TYPES`
  bound (until inc 2). No schema/route/money change → no balance gate; suite
  449→**453** python + 80 render.

- **Inc 2 done (Aug 5, 2026).** `agent_write_tools` now GENERATES each tool's
  `input_schema` from `actions.param_schema(verb)` — the hand-written schema
  blocks + the `_obj` helper + the `REAL_INCOME_TYPES` copy are deleted (128→94
  lines). The served schemas are byte-identical to before (inc 1 pinned
  `param_schema` == the old hand-written; the ask-write/loop/route tests confirm
  end-to-end). The single-source is now real: the income-type/kind/status
  vocabulary lives ONLY in `actions.py`. `test_action_schema` updated (the
  byte-equal test became "tools consume PARAM_SPECS"; the income-binding test
  became an unclassified-excluded check). No schema/route/money change → no gate;
  suite **453** python + 80 render.

- **Inc 3 done (Aug 5, 2026).** The MCP door: `ledger_mcp`'s write tools carried
  the income vocabulary in prose twice (`classify_inflow`'s `income_type`,
  `propose_income_rule`'s `set_type` — "One of: paycheck, …"). FastMCP builds its
  schemas from the function signature (not a schema you can hand it), so rather
  than force a whole-`param_schema` consume, the actual drift risk — the
  VOCABULARY — is now sourced from `actions.REAL_INCOME_TYPE_ORDER` via Pydantic
  `Field(json_schema_extra={"enum": …})`, and the type-list is dropped from the
  prose. Bonus: the MCP schema now ENFORCES the enum (prose never did — the verb
  was the only guard). Drift test in `test_agent_read_tools` (parallel to the
  read-tier descriptions drift test) asserts both tools' generated income enums
  equal the shared constant. No money/schema/route change → no gate; suite
  453→**454**. **The three vocabulary copies (verb constant + Ask enum + MCP
  prose×2) are now one source in `actions.py`, across BOTH doors.** Honest limit:
  FastMCP's signature-introspection means the parameter STRUCTURE (names/
  descriptions) still lives in the MCP signatures — descriptions legitimately
  differ per door (as the read tier already does); only the drift-prone vocabulary
  is unified.

**Action-schema build order steps 1–3 are DONE — the vocabulary is single-sourced
across the verb, the Ask door, and the MCP door, drift-tested at every seam.**
Optional remaining: inc 4 (verb-side first-pass structural validation from
`PARAM_SPECS`, must preserve the parity-pinned error strings) + inc 5 (generate/
coherence-check the CORE-DESIGN action-registry table). Neither is needed for the
drift-prevention payoff, which is fully banked.

**Batched deploy of the whole `rework` stack — main advanced (Aug 6, 2026).**
`main` → `d100cac` (`--no-ff` merge, first parent = prior main `e5b50bb`, tree ==
rework): budgets (Tier C, migration #010), mobile UI fixes, action-schema inc 1–3.
Full gate PASS — enumerated #010 diff only (`budgets` None→0 + `schema_version`
9→10), balance/monthly unchanged to the cent. Deployed by Alta (`#010 --live`).

**HOTFIX (Aug 6, 2026) — Analytics tab was blank post-deploy** ("Can't find
variable: budgetStatusHTML"). Root cause: budgets inc 4 added `budgetStatusHTML`
to `render.js` + called it (bare name) in `renderAnalytics`, but never added it to
app.js's `const { … } = window.Render` destructuring — so it was undefined at
runtime and `renderAnalytics` threw, blanking the whole tab. The node seam missed
it (it calls `R.budgetStatusHTML` directly, not through app.js's import). Fixed by
adding it to the destructuring, AND added a **build guard** in `test_render.js`
that statically asserts every Render helper app.js calls by bare name is
destructured from `window.Render` (verified it bites — fails with the exact
missing name). Frontend-only, zero-gate; render seam 80→**81**.

**Aug 6, 2026 — post-Foundry pivot: Agents tab, Ask expansion, ontology inc 1,
and the FRESH-START RESET (all deployed the same day).**
- **Foundry-comparison exploration closed:** ontology visualized (artifact
  `e8ac34b5…` + a Claude Design atlas); verdict recorded — the grammar is
  complete, remaining Foundry imports are judged per-need, lineage/ACL stay
  refused. **Ontology manifest inc 1 shipped** (per
  `docs/ONTOLOGY-MANIFEST-DESIGN.md`): `ontology.py` `manifest()` +
  `GOVERNED_TABLES` relocated test→`actions.py` + coherence tests (incl. the
  registry↔code check that closes ACTION-SCHEMA step 5). **Inc 2 (`GET
  /api/ontology`) not built yet** — the manifest is code-only, no route.
- **Agents tab v1 + polish — DEPLOYED.** `agent_catalog.py` (7 subagents
  coherence-tested vs `.claude/agents/`, model read from each file, + Ask/MCP/
  sync/guardian service entries, plain-language GLOSSARY), `GET /api/agents`
  (login-gated, shared — owner-gating was built then deliberately removed),
  Garden tiles in collapsible `<details>` groups w/ counts, collapsible "What
  the labels mean" Key (per-pill hover tooltip added then removed — the Key is
  the one explanation surface). 8th tab + 🤖 Home pill; mobile 5-slot nav
  unchanged.
- **Ask chatbot expansion — DEPLOYED (Alta's scope: budget_status read, quick-win
  pantry writes, money line HELD).** `ledger_budget_status` joined the shared
  read registry + `ledger_mcp` (reads 18→19, MCP 24 tools);
  `ledger_archive_item` + `ledger_set_item_match` joined `agent_write_tools`
  (writes 3→5), schemas generated from new `PARAM_SPECS` entries; system prompt
  gives the bot broad pantry freedom (lookup-first) and budget answers.
  Settle/edit/delete/rules remain out of Ask.
- **reset_money — DEPLOYED + EXECUTED on the Pi (Aug 6, 2026).** The household's
  numbers had drifted from the real accounts, so the ledger restarted from zero
  (Alta's call: clear money, keep setup, fresh bank links). CLI-only verb
  (registered row first; NO route — unreachable from UI/MCP/Ask; confirm phrase
  `reset all money rows`; one transaction; one audit row w/ before-counts;
  keep-set proven byte-identical in tests; runbook `deploy/reset-money.md`).
  Live run cleared: 134 transactions, 170 splits, 83 links, 1 bill_payment,
  1 goal_contribution, 1 pending_action; structure intact (`setup_required:
  false` post-reset, app 200). Pre-reset backup `finance.db.bak-pre-reset-<ts>`
  on the Pi is the undo. **The ledger now fills FORWARD from fresh SimpleFIN
  connections** — history does not replay (~10-day lookback window per claim);
  early numbers are recent-only and partial by design. NEXT: Alta re-claims
  SimpleFIN per bank (runbook step 4) as they connect accounts.
  Deploys: `main` `88e6d18` (Agents) → `9df0000` (Ask expansion + tooltip
  removal) → `de02dc7` (reset verb), each `--no-ff`, tree == rework, GATE PASS
  zero-diff, no migration. Suite **480** python + **90** render.
- **reset_money RUN AGAIN (Aug 8, 2026).** Second fresh-start wipe via the same
  CLI verb + `deploy/reset-money.md` — the Aug-6 reset's fresh bank re-claim was
  never done (the Jul-26 `simplefin_access.url` claim kept pulling), so Alta
  redid it clean: **keep the current banks**, let the twice-daily sync timer
  refill the empty ledger. Rehearsed on a `/tmp` copy first, then live-cleared
  **77 transactions, 124 splits, 61 links, 2 income_rules hit_counts zeroed**
  (bill_payments/goal_contributions/pending_actions all 0); structure kept intact
  (2 members, 4 bills, 1 goal, 33 pantry items). Pre-reset archive
  `finance.db.bak-pre-reset-2026-08-08-095321` (VACUUM INTO) — also copied off-Pi
  to the Mac `~/Ledger-archives/` (prune-proof; the Aug-6 pre-reset backup
  archived there too). Banks refill from the Jul-26 claim on the next timer runs
  (~18:08 same day, then 06:30 daily). No deploy — an operational data reset, not
  a code change.

**Aug 6, 2026 — ops/backup/sync hardening batch (deployed `main` `b8fa7e0`).**
Post-reset operational work; all off-Pi tooling / read-frontend, no schema/verb/
money path, zero-diff gates.
- **Ops panel on the Agents tab** — read-only in-app views: Pi guardian health
  badge + report (`GET /api/ops/health` reads `ops-status.txt`), recent audit
  activity (`GET /api/ops/audit`), both `login_required`, shared. An on-demand
  "Sync now" button + `POST /api/ops/sync` + a `record_sync_run` verb were built
  then DELETED after the SimpleFIN research (below) — kept the two read views.
- **SimpleFIN reality check (web-verified):** the Bridge refreshes bank data
  only ~once/24h and expects ≤~24 `/accounts` req/day; exceeding DISABLES the
  token. So: (a) no on-demand sync button (redundant + footgun); (b) `simplefin_
  sync.py` gained a **min-interval budget guard** (`SYNC_MIN_INTERVAL_S`, default
  1800s; clean no-op skip; `--force` for setup; `.last-sync` stamp by the db,
  gitignored so it can't dirty the Pi tree); (c) sync is now **twice daily
  (06:30 + 18:00)** — catches the daily refresh whenever it lands, 2 of 24, far
  above the guard interval. Hourly was rejected (same snapshot 24×, sits on the
  token-disabling ceiling).
- **Nightly backup** (`NIGHTLY-BACKUP-DESIGN.md`): `deploy/nightly-backup.sh` +
  `pifinance-nightly-backup.{service,timer}` @ 03:00 — a SEPARATE pool
  (`finance.db.nightly-*`, keep-newest `NIGHTLY_KEEP_BACKUPS`=14) so deploy
  bursts can't evict data snapshots; integrity-gated prune, read-only vs the db.
  Guardian gained a nightly check block AND **retired its deploy-pool age check**
  (a non-signal once nightlies exist). `*.db.nightly-*` + `.last-sync` gitignored.
- **Operator cheatsheet** — `deploy/CHEATSHEET.md` (full reference) +
  `deploy/pi-welcome.sh` (curated SSH-login greeting, wired via `~/.bashrc`,
  self-updating from the repo).
- **Ontology manifest** stayed at inc 1 (code-only `manifest()`); `GET
  /api/ontology` (inc 2) still not built.
- Suite **484** python + **92** render. **Manual Pi steps still pending** (NOT
  done by `deploy.sh`): install the nightly-backup systemd units + re-install the
  changed sync timer (sed-copy + daemon-reload), and the one-time `~/.bashrc`
  greeting wiring. Then tomorrow: **golden-backup refresh** (July-26 copy is
  pre-reset — dangerous) + **reconnect banks** (`--claim` per bank, then
  `simplefin_sync.py --force`).

**Mobile nav redesign + Ask-tab keyboard fix — DONE + DEPLOYED (Aug 6–7,
2026).** Two mobile complaints from Alta, one frontend-only increment. (1) The
5-slot mobile bottom bar only reached Home/Activity/Goals/Ask + Add — the other
four tabs (Bills/Analytics/Pantry/Agents) were reachable ONLY via Home shortcut
pills, so from any non-Home page you couldn't get to them. Redesigned to a
**pinned bar + "More" sheet** (Alta's pick from three options; pinned tabs =
Home/Activity/Ask, also Alta's call): bar is now **🏡 Home · 📋 Activity · [＋] ·
💬 Ask · ☰ More**, and **More** opens a rounded Garden bottom sheet
(`<dialog id="dlg-more" class="sheet">` + `moreSheetHTML` in render.js) listing
**all 8 tabs** as a 4×2 tile grid — so every tab is reachable from every page.
The active tab is highlighted in the sheet, and the **More button itself lights
green** when the current tab lives inside the sheet ("you are here"). Dropped the
now-redundant Home shortcut pills (+ their dead `.home-links` CSS). `MORE_TABS`
is derived from `TABS`, so a future 9th tab shows up in the sheet automatically.
(2) The Ask tab auto-focused its input on every render, popping the iOS keyboard
and hiding the example-question chips on open — now it focuses **only once a
conversation exists** (keeps the keyboard up for follow-ups) and NOT on the empty
tab. Frontend only — no schema/verb/derivation/route/money path → **no balance
gate**; render seam 92→**94** (moreSheetHTML: tile-per-tab + single active
highlight + no-false-highlight), python suite 484 green. Visually verified in the
in-app Browser (worked this session) via a throwaway harness rendering the REAL
`render.js` + `style.css` at 375px: bar layout, sheet open/close, tile→tab
switch, and the More "you are here" highlight all confirmed in **light AND dark**.
Committed `rework` `c7b19da`; deployed via `main` `544f7bc` (`--no-ff` merge,
first parent = prior main `b8fa7e0`, tree == rework). **DEPLOYED to the Pi (Aug 7,
2026)** — `deploy/deploy.sh origin/main b8fa7e0` → **GATE PASS zero-diff, no
migration** (schema stays v10), `pifinance` + `ledger-mcp` restarted; rollback
backup `finance.db.bak-2026-08-07-001329`. Verified live over the tailnet: new
nav (`moreSheetHTML`) served, dead `home-link` code gone, cache-busting stamping
the fresh `render.js?v=…`. Per-device hard refresh picks it up.

**Pi deploy footgun hit + fixed this deploy (Aug 7, 2026) — READ before the next
Pi deploy.** The Pi's git model: deploys run `deploy/deploy.sh origin/main`, whose
`git checkout origin/main` leaves **HEAD detached** at the deployed commit; the
Pi's local **`main` branch is NOT advanced by deploys**. The merge commit
`544f7bc` itself was made **correctly, on the Mac** (Alta ran it there — that's
why its first parent is `b8fa7e0` and its tree == rework; the Pi's stale
`main`@`e8f27d6` could never have produced that topology). The damage came from
running the **same** Mac-side merge command (`git checkout main && git merge
--no-ff rework && git push … && git checkout rework`) a **second time on the Pi**
— in a separate chat that lacked this Mac-vs-Pi context. On the Pi its `git merge`
step did nothing useful (stale `main`), but its leading `git checkout main`
reverted the whole working tree to the Pi's stale local `main` — which was
pinned all the way back at **`e8f27d6` (schema v3, July)** — so on-disk `app.py`
became v3 (`REQUIRED_SCHEMA_VERSION=3`), `deploy.sh` vanished (didn't exist at
v3), and the app kept serving only because gunicorn still held the v10 code in
memory. **Latent outage:** any `systemctl restart`/reboot would have loaded v3
code, which hard-rejects the v10 DB (`require_current_schema` wants history
`[1,2,3]`, DB is `[1..10]`) → workers die. `finance.db` was never at risk (it's
gitignored, untouched by any checkout). Recovery (all Pi-side): (1) `git checkout
origin/main` — restore disk to v10, **no restart, zero downtime**; (2)
`deploy/deploy.sh origin/main b8fa7e0` — the explicit old-ref `b8fa7e0` is what
made the gate compare v10-vs-v10 (real frontend diff) instead of the v3-vs-v10
mismatch a bare `deploy.sh origin/main` would have hit; (3) `git branch -f main
origin/main` — **heal the stale local `main`** so a future `git checkout main`
can't revert to v3 again (now done — local `main` == `origin/main`). **Lessons:**
Mac-side git (merge/push to `main`) and Pi-side git (`deploy.sh`) are separate —
LABEL which machine each command is for; never run the merge command on the Pi.
When a Pi deploy's gate would compare against the wrong baseline, pass the
actually-deployed commit as `deploy.sh origin/main <old-ref>`. Cross-session
detail: memory [[ledger-workflow]].

**REDVAULT security squad + team codenames — DONE, NOT DEPLOYED (Aug 7, 2026).**
Seven new role-scoped subagents in `.claude/agents/` forming an authorized,
defensive pen-test → verify → patch → re-test pipeline over the household's OWN
Ledger stack (concept artifact `fbc269c3-03c3-4298-b6f3-402653230ac3`):
`ledger-scout` (SCOUT · recon/scope-marshal, read-only), `ledger-picklock`
(PICKLOCK · auth/session, dev copy), `ledger-mirage` (MIRAGE · injection +
prompt-injection of the Ask/MCP tool boundary, dev copy), `ledger-keyring`
(KEYRING · access-control/IDOR + two-phase-write replay, dev copy),
`ledger-blackout` (BLACKOUT · infra/secrets/exposure, recommend-only on the Pi
like `ledger-ops`), `ledger-patchwright` (PATCHWRIGHT · fix author — edits `rework`
+ a regression test proven to fail-first, stops before commit like
`ledger-maintenance`), `ledger-tribunal` (TRIBUNAL · verifier/purple-lead —
adversarially confirms findings, runs the closed loop where the original finder
re-attacks the patch, hands Alta the go/no-go). All bounded by
`docs/OPERATING-CHARTER.md`: own assets only, `dev.db` copies never `finance.db`,
recommend-only on the live Pi, every fix clears the balance gate, no agent deploys.
Tools scoped per role (only PATCHWRIGHT gets Edit/Write; MIRAGE/BLACKOUT get
WebSearch/WebFetch). Registered in `agent_catalog.py` (SUBAGENTS 7→14, new
`redvault` group, +1 glossary label `dev copy`) so the coherence test
(`test_subagents_match_agent_files`) stays green, and the standing seven each got a
tactical codename too (ORACLE/WARDEN/QUILL/KEEPER/GATEKEEPER/PULSE/BEACON) in both
the catalog display name and their file. Tooling/frontend only — no schema/verb/
derivation/money path → **no balance gate**; suite 484 python + 94 render green.
The in-app Agents tab now shows the REDVAULT group + codenames once deployed.

**CODE REVIEW + REMEDIATION — every P0 and P1 shipped (Aug 7, 2026).** A
three-pass adversarial review of `rework` (security / architecture-quality /
test-quality subagents), written up in `docs/CODE-REVIEW-2026-08-07.md` (findings,
failure scenarios, fixes, live remediation-status blocks). Every finding verified
against the real code before landing; every regression-critical guard watched to
FAIL on the old code first. Suite 484 → **499 python + 96 render**. Shipped across
three gated deploys — `main` `bda68a3` → `346b838` → `3de39fc`, each `--no-ff`,
first parent = prior main, tree == rework, **GATE PASS zero-diff, no migration**.
- **P0-1 stored XSS — FIXED + LIVE.** `static/render.js` `esc()` escaped only
  `&<>`, so a `"`-bearing item name / budget category broke out of the aria-label
  attributes into a live event handler (items/categories are household-shared →
  one member's payload runs in the other's privileged session; same-origin fetch
  could mint a `read,write` token). `esc()` now escapes both quote forms; unit +
  attribute-breakout regression, both proven to fail on the old code.
- **P0-2 unauthenticated MCP port — CLOSED (Tailscale ACL).** `ledger_mcp` ran
  with NO inbound auth (probed live: `100.108.237.13:8765` answers
  unauthenticated), so any tailnet peer — incl. Charlee's device — could drive all
  24 tools + both halves of the two-phase write tier under the Pi's `read,write`
  token. Closed with a tailnet ACL (grants format, `deploy/mcp-tailnet-acl.md`):
  `altamashmomin@github → *:*` (Alta's devices full), everyone else →
  `100.108.237.13:8080` ONLY, so `:8765` is Alta-only. Built-in `tests` block
  makes the deny provable — a clean save == the "Charlee can't reach :8765" test
  passed. NOTE: Charlee is a Tailscale ADMIN (kept — Alta's call, useful for phone
  support); the port is closed to her *device* regardless of role (network access
  is grant-governed, not role-governed), so the posture holds — her *account*
  could edit the policy, an accepted trust decision.
- **P0-3 LAN exposure — CLOSED (BIND_HOST + dual-bind).** gunicorn bound `0.0.0.0`
  (tracked `pifinance.service`), exposing a plaintext-http login on the home LAN.
  Introduced `BIND_HOST`; the Pi's `.env` sets the tailnet IP. **Same-day
  follow-up bug + fix:** binding ONLY the tailnet IP broke Pi-local clients —
  `ledger_mcp` reaches Flask over `LEDGER_API_BASE=http://127.0.0.1:8080` and
  `deploy.sh`'s health check probes loopback; both went dark (silent since the
  P0-3 step — the deploy WARNING surfaced it). The unit now ALWAYS binds loopback
  PLUS the optional `BIND_HOST` interface (`ExecStart=/bin/sh -c` with
  `${BIND_HOST:+…}`, `$$`-escaped so the shell does the conditional); LAN still
  never bound. Verified on the Pi: two listeners `127.0.0.1:8080` +
  `100.108.237.13:8080`, no `0.0.0.0`; loopback + tailnet both 200; `ledger-mcp
  active`. `After=tailscaled.service` added so the tailnet-IP bind survives a
  reboot. **Lesson: a service binding a specific non-loopback IP must ALSO bind
  loopback, or Pi-local siblings + the deploy health check break** [[ledger-ops-layer]].
- **P1 — all correctness findings FIXED + LIVE.** Live bearer-token 500 in
  `pay_bill`/`contribute` (`session["user_id"]` → `g.auth["user_id"]`, + bearer
  tests); `finance.db` guards made case-insensitive + symlink-resolving
  (`basename(realpath).lower()` in `seed_db`/`seed_income`/`migrate`/`gate`);
  `WRITE_RE` widened (INSERT OR REPLACE/IGNORE, REPLACE INTO, DROP/ALTER TABLE) +
  full-text scan; CSP + `nosniff` + Referrer-Policy via `after_request`; a JSON
  `errorhandler(Exception)` so a backend 500 can't blank a tab; login
  timing-oracle closed (dummy-hash) and the login route finally tested; **rate
  limiting** (in-process fixed-window: `/api/login` 10 fails/15min reset-on-
  success, `/api/ask` 30/hr/user → 429; per-worker with 4 workers, accepted for a
  two-person home app); the **derivation tripwire made non-vacuous** (fixture now
  seeds `items` + a matching in-window shopping-category probe → removing the
  `direction='out'` filters trips **1→8** functions under mutation; the "tripwire-
  covered" overclaim corrected at its definitional point in Conventions); the
  **balance gate** gained optional `by_category`/`income` snapshot sections
  (catches a category reassignment / income misclassification the old net-only
  snapshot missed; v1.0-baseline path preserved via compare-only-when-both-have-
  it); the **txn-list N+1** killed (`actions.prefetch_payer_shares` → `/api/
  transactions`, `/api/activity`, dashboard-recent issue ONE splits query, was up
  to ~502; byte-parity held); the **render build-guard** given the inverse
  direction (resolves every bare call in `app.js` → catches a call to a name that
  exists nowhere, the real blank-tab outage class); **#7 prompt-injection
  hardening** (Ask system prompt leads with an untrusted-data rule, every tool
  result labeled `[untrusted tool data …]` in the loop, refund tagging requires
  explicit user say-so since it's the one Ask write that moves a spend total).
- **Capacity:** service unit → `--workers 4 --timeout 120` (Pi 5 has 4 cores;
  `/api/ask` holds a worker for the whole model loop).
- **Deploy footgun (again):** the propagation race bit the FIRST deploy attempt —
  its internal `git fetch` grabbed the prior `main`, gated old-vs-old (GATE PASS
  but shipped nothing, HEAD stuck at `544f7bc`). Fixed by fetching to confirm
  `origin/main` propagated, then `deploy.sh origin/main <currently-deployed-ref>`
  with the explicit old-ref so the gate compares the right baseline [[ledger-workflow]].
- **#16 perf sub-path 1 — DONE + DEPLOYED (Aug 7, `main` `e699444`).** The pantry
  N+1: `/api/inventory`'s five staple-looping derivations (`restock_suggestions`,
  `restock_forecast`, `unmatched_staples`, `stale_shopping_items`, `staple_spend`)
  each did one `instr()` table scan per staple (~150 with 30 staples). Now they
  share one `_purchase_index(db)` scan (all outflows, pre-sorted) that
  `_matching_purchases` filters in Python when passed `index=`; the per-item SQL
  path stays for lone calls. Byte-identical (parity test index==SQL, balance GATE
  PASS 42 values zero-diff, measured 125→5 scans). Suite 500 python + 96 render.
- **#16 perf sub-path 2 — DELIBERATELY SKIPPED (Alta's call, Aug 7).**
  `_monthly_series`'s per-month `spending_summary` (income/category trends). On the
  occasional analytics tab it saves ~10 fast queries but a clean fix touches three
  MONEY derivations (`income_summary`/`income_trend`/`category_trend`) with real
  parity risk — cost/benefit doesn't justify it. NOT a gap; a decision.
  **The Aug-7 code review is now fully closed** — every P0, every P1, and the one
  #16 sub-path worth doing, all shipped. Nothing outstanding from it.

**iOS add-expense dialog fix + user-set pantry restock cadence — DONE + DEPLOYED
(Aug 7, 2026).** Two aesthetics/UX items Alta reported from an iPhone, built as
two separate commits on `rework`, shipped together in one batched deploy.
**Deployed via `main` `faccf9d`** (`--no-ff` merge, first parent = prior main
`c1a5439`, tree byte-identical to rework; fast-forward push). `deploy.sh
origin/main` on the Pi (baseline defaulted to the deployed HEAD `e699444` — a
v10 commit; `origin/main`'s earlier `c1a5439` was a docs-only merge the Pi had
never deployed, so nothing was skipped) → **GATE PASS**, enumerated `#011` diff
only (`schema_version` 10→11; balance + every monthly total unchanged to the
cent), `#011` applied `--live`, `pifinance` + `ledger-mcp` restarted clean,
`/api/status` OK. Rollback backup `finance.db.bak-2026-08-07-194546` (retention
prune kept newest 10). Per-device hard-refresh picks up the frontend. First
`--live` migration since #010.
- **iOS date/amount overlap (`586360d`, frontend-only, no gate).** In the
  add-expense dialog the Date and Amount fields overlapped on iOS Safari. Root
  cause: iOS renders `input[type=date]` with its native control, which keeps an
  intrinsic content width and IGNORES `width:100%` inside the `.row2` 1fr grid
  column, so the date field overflowed into amount. The Aug-5 `min-width:0` fix
  only helps standard browsers (Chromium at 375px was already clean, confirmed
  in a harness) — iOS needed `-webkit-appearance:none` to honor the width/box
  model. Scoped to the mobile layout (`@media (max-width: 719.98px)`, below the
  720px desktop breakpoint) so desktop keeps its native calendar-picker button.
  Covers every date dialog (bill/goal share `.row2`). render seam 97.
- **#011 — user-set restock cadence (`168c38f`, gated migration).** Answers
  Alta's "I don't understand where the pantry gets its intervals from": today
  they're purely INFERRED (`restock_forecast` = median gap between bank-feed
  matched purchases, ≥3 needed) — invisible and sparse (merchant-not-product).
  Now a person can SET one. Migration #011 adds `items.restock_interval_days`
  (nullable; NULL = infer, prior behavior) + `items.last_stocked_at` (the anchor
  the manual cadence counts from — Alta's chosen anchor: "last time marked
  stocked"; `add_item`/`set_item_status` write it on a stock event). New verb
  `set_item_interval` (staple-only, 1..365 or None-to-clear, audited, UI-only —
  not exposed to Ask/MCP this increment); `restock_forecast` gains a MANUAL
  branch (`predicted = last_stocked_at + N`, no purchase history needed) beside
  the CADENCE branch, each row carrying `interval_source`. Pantry UI: a per-
  staple ⏰ "remind every N days" editor + hint line, and the "Coming up" card
  now attributes the number — "(you set this)" vs "(from your purchases)". Still
  clock-free (anchor+interval from stored data) → tripwire covers it, no
  exemption; never touches money. **Enumerated-diff balance GATE PASS** (`586360d`
  → `168c38f`, seeded v10 db: only `schema_version` 10→11, balance + every
  monthly total unchanged to the cent; notes/011). Suite 500→**510** python + 96→
  **97** render. **Deploy: this is the first `--live` migration since #010** —
  advance `main` to rework, `deploy.sh origin/main` runs its dry-run gate (prints
  the #011 structural diff to eyeball vs notes/011), applies `#011 --live`,
  restarts. Per-device hard-refresh picks up the frontend.

**Ask-tab chat cadence for Charlee — DONE + DEPLOYED (Aug 7, 2026).** Deployed via
`main` `873a78d` (`--no-ff` merge, first parent = prior main `faccf9d`, tree
byte-identical to rework; merged in an isolated worktree to avoid disturbing
Alta's uncommitted `/trace` WIP in the working dir). `deploy.sh origin/main` on
the Pi → **GATE PASS, zero diff** (no migration — schema stays v11 — no money
moved), `pifinance` + `ledger-mcp` restarted clean, `/api/status` OK. Rollback
backup `finance.db.bak-2026-08-07-212724`. No per-device refresh (backend +
system-prompt only). The
follow-on to #011: let Charlee set a restock cadence by *chatting* ("remind me to
restock coffee every two weeks"), not just via the ⏰ editor. Scoped to her door
only (Ask), like the other pantry writes — the MCP write tier (Alta's door) for
this stays a later increment if ever wanted. No new verb/route/schema: it bottoms
out in the same `set_item_interval` verb via `PUT /api/inventory/<id>` the SPA
uses (one write path, `ui:<name>`, logged, reversible). Changes: a `PARAM_SPECS`
entry for `set_item_interval` (item_id + days, generated schema — the verb was
UI-only at #011); `ledger_set_item_interval` in `agent_write_tools` (writes 5→6,
so the Ask loop offers 19 read + **6** write = 25 tools); Ask system prompt gains
the cadence capability + example. `test_ask_write` proves the tool flips
`restock_interval_days` through the route as `ui:avery` and that an out-of-range
value (9999) is caught by the verb (not written) and recoverable; `test_action_schema`'s
VERB_TOOL map + the tool-count assertions updated. Pure client of the already-gated
inventory route → **no balance gate**; suite 510→**512** python + 97 render.
**Deploy is a plain frontend/agent path** (no migration, schema stays v11):
advance `main` to rework, `deploy.sh origin/main <deployed-ref>`, GATE PASS
zero-diff, `ledger-mcp` restarts (no new MCP tool, but the restart is harmless).
Prereq already met: `ANTHROPIC_API_KEY` on the Pi (⚠ expires ~Aug 30). No
per-device refresh needed for this one (backend + prompt only — the ⏰ editor UI
already shipped with #011).

**`/trace` — the architecture Trace Web — DONE + DEPLOYED (Aug 7, 2026).** A
static, self-contained interactive map of the ontology served at `/trace`
(callers → verbs → objects → derivations → doors), click-to-isolate any node's
full read/write path. Ungated like the rest of the frontend (carries no
household data, only the code's shape); its edge logic is a same-origin script,
so the strict `script-src 'self'` CSP holds; the shell version-stamps the script
and injects the live schema version. **Self-verifying**: `tests/test_trace_web_data.py`
parses the map's data and pins it to the source at BOTH levels — node membership
(verbs↔`ontology.manifest()`, objects↔`GOVERNED_TABLES`, derivations↔`derivations.py`)
AND the wiring (every verb→object write proven in actions.py, every
object→derivation read proven in derivations.py, the doors↔`manifest.doors`), so
it can't silently drift from the code. Building the edge guard surfaced + fixed
two real edges the hand-drawn map had missed (`record_transaction`→`income_rules`
hit_count bump; `confirm_action`→`transactions`+`income_rules` via
`_apply_single_rule`) and a stale door count. Every guard verified to bite. The
map reconciled to current source (29 verbs incl. `set_item_interval`/`reset_money`,
the latter on a distinct `CLI · maintenance` caller — the one write path bypassing
every door; footer counts computed in-page). Frontend + one ungated read route —
no schema/verb/derivation/money path, no balance gate; suite **526** python + 97
render. Deployed via `main` `f3d086f` (`--no-ff` merge, first parent = prior main
`873a78d`, tree == rework, merged in an isolated worktree to avoid disturbing the
then-uncommitted WIP; local `main` synced to `origin/main` afterward). `deploy.sh
origin/main` → **GATE PASS zero-diff, no migration** (schema stays v11),
`pifinance` + `ledger-mcp` restarted, `/api/status` OK; rollback backup
`finance.db.bak-2026-08-07-223107`. **Live at `http://raspberrypi:8080/trace`**
over the tailnet (session-gated as of Aug 8, 2026 — see the code-review fix
below; originally shipped ungated). **Discoverable from the Agents tab (Aug 8, 2026):** a Garden `.trace-link` card
at the top of the Agents tab (`agentsHTML`) links to `/trace`, opening in a new
tab so the SPA keeps its place (the map page has no back). Frontend only, render
seam 97→98, no gate; deployed via `main` `243e315` (`--no-ff`, first parent =
prior main `f3d086f`, tree == rework) → GATE PASS zero-diff, `/api/status` OK.
One-time per-device hard refresh to pick up `render.js`/`style.css`.
Remaining optional future (#3, not built):
data-drive the map from a live `GET /api/ontology` so it needs no guard at all —
a separate feature that needs that endpoint built first (ontology-manifest inc 2).

**Agent roster pruned 14 → 6 (Aug 8, 2026), after a full codebase review.** The
comprehensive review (six parallel domain reviewers + a security re-audit; PDF
delivered to Alta) found the roster ~3× over-scaled for a two-person app — the
Operating Charter governed only 7 of 14, and the two "manager" roles
(chief-of-staff, tribunal) structurally can't supervise (an agent can't spawn an
agent). Kept the lean set: `ledger-analyst`, `ledger-release`,
`ledger-health-sweep`, `ledger-maintenance` (now also authors backend/security
fixes — `patchwright` folded in), `ledger-ops`, and one red-teamer `ledger-mirage`
(the injection/agent boundary is the newest, least-covered surface). Deleted 8:
`security` (routine checks fold into health-sweep; deep audits on-demand),
`chief-of-staff`, and the six other REDVAULT agents (`scout`/`picklock`/`keyring`/
`blackout`/`patchwright`/`tribunal`) — a full pentest squad is now spun up
on-demand, not kept standing. Also dropped the codename ceremony (plain role names).
`agent_catalog.py` SUBAGENTS/GROUPS updated in the same change (the `redvault`
group became `security`), `test_agents.py`'s model assertion repointed off the
deleted `ledger-security`, and cross-references in the kept files + the Charter's
separation-of-duties table reconciled. Frontend/tooling only — no schema/verb/
derivation/money path → **no balance gate**; suite 526 python + 98 render green.
Committed `rework` `6db9273`; **DEPLOYED to the Pi (Aug 8, 2026)** via `main`
`a57ff79` (`--no-ff` merge, first parent = prior main `243e315`, second parent =
`6db9273`, tree byte-identical to rework; fast-forward push — the Mac-side merge was
run on the Mac, not the Pi). `deploy/deploy.sh origin/main 243e315` → **GATE PASS,
zero diff, no migration** (schema stays v11), `pifinance` + `ledger-mcp` restarted
clean, `/api/status` 200. Rollback backup `finance.db.bak-2026-08-08-133350`
(retention pruned to newest 10). The in-app Agents tab now shows 6 (one-time
per-device hard refresh to pick up the roster).
**member_breakdown cent bug — FIXED + DEPLOYED (Aug 8, 2026).** The first
code-review finding actioned. The Analytics per-member paid/owed/net card rounded
EVERY member's share independently, so a shared expense that didn't divide to whole
cents (a 101¢ 50/50 split) leaked the residual into the payer's net — per-member
nets summed to ±1 not 0, disagreeing with `compute_balance` by a cent. (The core
who-owes-whom balance was always correct; `compute_balance` already rounded only the
non-payer share.) Fix mirrors `compute_balance`: within each transaction round only
the non-payer shares and let the PAYER absorb the residual, so per-txn shares sum to
the whole. Regression tests (101¢ + 103¢, both rounding parities) proven to FAIL on
the old code first, pinning conservation AND agreement-with-balance. Read-time
derivation only — `member_breakdown` is NOT in the gate's snapshot and no gated
function changed → **zero-diff balance gate PASS** (36 values, seeded dev.db,
`a57ff79`→`rework`). Suite 526→**528** python + 98 render. Committed `rework`
`104b074`, **DEPLOYED** via `main` `73e963f` (`--no-ff` merge, first parent = prior
main `a57ff79`, tree == rework), `deploy.sh origin/main a57ff79` → GATE PASS
zero-diff, no migration (schema stays v11); `pifinance` + `ledger-mcp` restarted,
`/api/status` 200. Rollback backup `finance.db.bak-2026-08-08-224638`.
**`/trace` auth — FIXED + DEPLOYED (Aug 9, 2026).** Code-review finding
P3-1: `/trace` served the full internal data model (every verb, table, derivation,
door, two-phase target) with no auth. Gating the HTML route alone was insufficient —
the model actually lives in `static/trace-web.js`, static-served at the root
(`static_url_path=""`), so it was directly fetchable. Fix is a single
`_gate_trace_web` `before_request` hook covering BOTH `/trace` and `/trace-web.*`:
session-only (the map is a human page; bearer/MCP clients read the model through the
tools, never this page), redirecting an unauthenticated browser to the SPA login
`/`. The Agents-tab link still works — a logged-in browser carries the session
cookie to the new tab. Regression test proven to FAIL first (gate neutralized →
`302 != 200`): unauthenticated `/trace` AND `/trace-web.js` both redirect and leak
neither `confirm_action` nor `buildEdges`; existing trace tests now authenticate via
`session_transaction`. Route/auth change only — no schema, verb, derivation, or money
path → **no balance gate** (like the cache-busting / mobile-nav frontend increments).
Suite 528→**529** python + 98 render. Changed `app.py` (+ `redirect` import) and
`tests/test_trace_route.py`. **DEPLOYED** via `main` `fd15244` (`--no-ff` merge, first
parent = prior main `73e963f`, tree == rework), `deploy.sh origin/main 73e963f` →
**GATE PASS zero-diff, no migration** (schema stays v11); `pifinance` + `ledger-mcp`
restarted, `/api/status` OK. Rollback backup `finance.db.bak-2026-08-09-084502`.
**Deploy + MCP P1 hardening — DONE + DEPLOYED (Aug 9, 2026).**
The two code-review P1s, in two increments; tooling/deploy/MCP-server only — no
app/schema/derivation/money path, so **no balance gate**. Suite 529→**530** + 98 render.
DEPLOYED via `main` `c5989e2` (`--no-ff` merge, first parent = prior main `fd15244`,
tree == rework), `deploy.sh origin/main fd15244` → **GATE PASS zero-diff, no
migration** (schema stays v11); `pifinance` + `ledger-mcp` restarted, `/api/status`
OK. Rollback backup `finance.db.bak-2026-08-09-210811`. Alta added
`LEDGER_MCP_ENABLE_WRITES=1` to the Pi `.env` before the deploy, and the MCP log
confirmed `write tier ENABLED` on restart (write tier preserved). This deploy ran
the OLD deploy.sh (self-replace quirk, as expected — the no-op-guard/heal/fatal-smoke
lines were absent from the output); the hardened deploy.sh takes effect next deploy.
**Still pending (Alta's, off-repo): verify the tailnet ACL from a NON-owner device**
— the Pi-local `curl :8765` returns a healthy MCP response but does NOT test the ACL;
from Charlee's phone the port should fail to connect (deploy/mcp-tailnet-acl.md).
- **deploy.sh footgun (P1-B).** `deploy.sh` did `git fetch` then a bare `git checkout`,
  never advancing local `main`, so both spellings could ship stale code (bare `main` =
  stale local branch → false GATE PASS; `origin/main` = detached HEAD leaving local
  `main` stale → the schema-v3 revert trap), and a failed post-apply smoke check only
  WARNed and exited 0 (masking a crash-loop till the 07:00 guardian). Fixes, all
  preserving the backup/gate/rollback flow: (1) a **no-op/propagation-race guard** —
  after fetch, if the resolved target SHA == the currently-deployed SHA, die instead of
  "deploying" the running code under a misleading PASS; (2) **heal the local branch** to
  the deployed commit on success, so a later `git checkout main` can't revert to stale
  code; (3) the **smoke check is now FATAL** — prints the rollback and exits non-zero,
  with a 15s startup retry loop. `pi-deploy.md` reconciled to the canonical
  `origin/main <deployed-sha>` (was a stale `deploy.sh main` on `/home/pi`). Git logic
  unit-tested off-Pi in a scratch repo; `bash -n` clean. **Quirk:** deploy.sh
  self-replaces mid-run, so these take effect on the deploy AFTER the one that ships them.
- **MCP write tier had no inbound auth (P1-A).** `ledger_mcp` ran the streamable-HTTP
  transport with no verifier — the only boundary was the out-of-repo tailnet ACL, and a
  `read,write` token meant any tailnet peer reaching `:8765` could drive writes. Added a
  **safe-by-default opt-in**: `_writes_enabled()` gates the single write choke point
  `api_write`, so **every MCP write is refused unless `LEDGER_MCP_ENABLE_WRITES` is set on
  the server** — a reachable port or a leaked token is no longer enough to change data
  (reads unaffected; this is MCP-door only — Charlee's Ask writes go through the session,
  untouched). Plus a **loud startup warning** when bound beyond loopback (no inbound auth
  → the tailnet ACL is the boundary) and a write-mode log line. `test_ledger_mcp_write`
  proves a `read,write` token is STILL refused with the flag unset (nothing written);
  `.env.example` + `deploy/mcp-write-tier.md` document the flag and the ACL-verify step.
  **This is defense-in-depth, not full closure**: the primary control is still the tailnet
  ACL (Alta's to verify live), and per-client inbound auth would need a bigger FastMCP
  change. **⚠ Operational step on deploy: Alta must add `LEDGER_MCP_ENABLE_WRITES=1` to the
  Pi `.env` (beside the restart) or the MCP write tier goes read-only.** Deploy is a plain
  frontend/tooling path (no migration); deploy.sh's own change lands a deploy later (the
  self-replace quirk). Remaining review findings (CLAUDE.md/test-infra bloat) — now
  actioned, see below.

**Repo-hygiene bloat cleanups — DONE on `rework` (Aug 9, 2026).** The last two
code-review findings (both P1-maintainability, no code/money path — no gate). Two
increments:
- **CLAUDE.md split (`eb9f6d1`).** CLAUDE.md was 2171 lines / 145 KB, ~95% an
  append-only journal loaded into context every session (~36 KB) while the
  load-bearing rules are <120 lines. Moved the "Current position in the sequence"
  journal body to **this file, `docs/PROGRESS-LOG.md`** (history preserved in git),
  leaving CLAUDE.md at 136 lines: the hard rules, per-increment loop, balance gate, a
  compact current-state pointer, and the conventions. New increment records append
  here now, not to CLAUDE.md.
- **Shared cached test fixture (`7ee09b2`).** Every test method's `setUp` shelled out
  to `seed_db.py` + `migrate.py` — ~1,000 subprocess spawns across the suite, most of
  its wall time. New `tests/_seedbase.py` builds each distinct `(seed, months, as_of)`
  template ONCE per process and file-copies it per test (byte-identical to a fresh
  build — fixtures are deterministic — so every test sees the same data on its own
  private copy). Converted 55 files via a one-shot transform verified by the full
  suite; left the 7 income files (they lean on `seed_income.py`'s own arg defaults,
  which don't map onto a shared key) and 4 bespoke-helper files untouched. Suite
  unchanged at **530 green**; **runtime ~64s → ~32s**. Dropped the now-unused
  `import subprocess` from the converted files. **All code-review findings are now
  closed** (the two deploy/MCP P1s' one remaining item is Alta's off-repo ACL check).

**`GET /api/ontology` + data-driven Trace Web — DONE on `rework` (Aug 10, 2026,
`f757174`).** The Trace Web's optional #3 (from the Aug-8 build) and
ONTOLOGY-MANIFEST-DESIGN's increment 2, together: the manifest becomes the map's
single source. `/trace` now fetches `/api/ontology` at load and draws whatever it
reports, so the map's facts CANNOT drift from the code — the old hand-kept edge
tables in `trace-web.js` and their sync guards are obsolete by construction. Only
presentation hints remain in the script (visual grouping, preferred ordering,
display labels, phase/sink styling, and the one FK-cascade edge — `delete_goal` →
`goal_contributions` — that SQLite enforces rather than code; a hint that goes
stale fails `test_trace_web_data`, now repurposed as the hints guard, incl. a DDL
check that the FK is still real).
- **`ontology.py` grew the edge-level facts**, each derived from source:
  `actions[].writes_direct` (a verb's OWN footprint — body + private helpers,
  stopping at other verbs; `writes` stays the full closure), `functions[].reads`
  (FROM/JOIN closure through derivations' helpers), `actions[].cascades`
  (verb→verb dispatch), and `callers` (ui/sync/mcp/ask/cli — the agent doors
  derived by walking their HTTP calls through app.py's route table; `cli` =
  verbs reachable from no surface, i.e. `reset_money`).
- **Two real derivation bugs found & fixed while building it**: (1) docstring
  prose registered as calls — `create_income_rule` was being charged with
  `confirm_action`'s writes off the prose mention "…propose_action/confirm_action
  (the MCP write tier)"; fixed with AST call detection over docstring-stripped
  source, applied everywhere incl. `_two_phase_targets`. (2) Paren-less verb
  references were missed — `actions.contribute_to_goal if cents > 0 else
  actions.withdraw_from_goal` — fixed with a reference (not call) scan for
  routes. Plus `reset_money` is now charged with `RESET_TABLES` (its f-string
  DELETE loop is invisible to the text scan; the constant itself is importable
  source, so the fact stays derived). `transactions.written_by` now honestly
  includes `reset_money`.
- **`/api/ontology` is `login_required`** — the code's shape is reconnaissance
  surface (CODE-REVIEW-2026-08-08 #P3-1); this is the API-shaped equivalent of
  `/trace`'s session gate (401 JSON; bearer read qualifies like every /api GET).
- The map's idle readout is now fully computed (widest write surface, "never
  read by a derivation" — which correctly surfaces links & income_rules beside
  the three sinks — and "no write verb: members"). Provenance line reads "Live
  from /api/ontology — 29 verbs · 14 tables · 23 derivations · schema v11".
  Verified visually via a fetch-stubbed harness fed the real manifest: 74 nodes ·
  233 edges (4 more honest edges than the hand map), CLI trace isolates only
  reset_money, confirm_action shows own-writes + phase + cascades.
- Manifest edge pins in `test_ontology` were chosen to bite on the exact bugs
  above; `test_trace_route` covers the endpoint, its gate, and the fetch wiring.
  Backend read route + frontend — no schema/verb/derivation/money path, **no
  balance gate**. Suite **536** python + **98** render. NOT YET DEPLOYED.
