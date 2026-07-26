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
   an enumerated-diff demonstration (`notes/006-gate-expectation.seed.json`)
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
7. Analytics tab + income-vs-spend chart — new nav tab; hand-rolled SVG
   in the app's bespoke style (no chart lib — CSP + no build step).

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
8.  Category trend — per-category monthly series + 3-mo rolling average +
    MoM delta (`transactions.category` × `monthly_series`).
9.  Savings-rate trend — `income_summary.savings_rate` across months; the
    one line Charlee-facing analytics leads with.
10. Category mix + top merchants — share-of-spend composition for a month,
    and description-grouped top-N by spend.
11. Per-member view — each person's paid-vs-owed share over time from
    `splits` (basis points) × `paid_by`.
12. Bill-vs-actual variance — defined `bills.amount` vs what
    `bill_payments`/their transactions actually cost.

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

Next feature increment: **step 3 increment 7 — analytics tab +
income-vs-spend chart**. New nav tab; hand-rolled SVG in the app's bespoke
style (no chart lib — CSP + no build step), consuming `GET
/api/income/trend`. The SVG scaling/path math goes in `render.js` pure
helpers (testable in the `node tests/test_render.js` seam — exactly the
kind of pure function that belongs there); frontend gets a live-app browser
check. Then the deeper-analytics extensions 8–16 above (Tier A first) —
each new `*_trend` rides the `_monthly_series` engine inc 6 just built
(pass a different `metric_fn`).

Cosmetic follow-up surfaced by inc 5 (low priority): now that a month's
Spent can go negative (a refund exceeding that month's spend), the
dashboard renders it `$-353.51` — `Render.fmt` puts the minus after the
`$`. Prefer `−$353.51`. Only reachable in the refund-heavy edge; not a
correctness issue.

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
