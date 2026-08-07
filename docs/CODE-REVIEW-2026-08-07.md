# Code Review — August 7, 2026

Full-codebase proofread of `rework` @ `a53aae5`, run as three parallel
adversarial passes (security, architecture/quality, test quality) with every
significant claim independently verified before it was recorded here.

**Read-only review.** No file was edited, no git state was mutated, nothing was
deployed. The one active probe was an unauthenticated HTTP reachability check
against the Pi's own MCP port (finding 2) — no tool was invoked and no data read.

**Baseline at review time:** 484 Python tests + 94 render checks, all green.
`pip-audit -r requirements.txt` → no known vulnerabilities.

---

## Verdict

This is an unusually disciplined codebase. The constitution in `CORE-DESIGN.md`
is not decoration — invariants 1, 3, 4, 6 and 7 are mechanically enforced by
tests that were each watched to fail once. Under adversarial reading from three
directions, the **verbs, the SQL, the scope model and the audit trail all held**.

The faults cluster in two revealing places:

1. **The edges the invariants don't reach** — the browser (`esc`), the MCP
   transport (outside Flask's auth), the systemd unit (outside the code), and
   the model's context window (outside the verb contract).
2. **The meta-layer** — the tripwire, the raw-write regex, the render build
   guard, the balance gate's snapshot scope, and several coherence tests are
   each trusted in the docs at a strength they do not have.

The second is the more valuable finding, because it is invisible from inside a
green suite.

---

## P0 — Fix before anything else

> **Remediation status (updated in-session):**
> - **P0-1 — FIXED & VERIFIED.** `esc()` now escapes both quote forms
>   (`static/render.js:27`); the vulnerable pinning test was corrected to the new
>   contract and two regression tests added (a unit check + an attribute-breakout
>   check against the real `inventoryHTML`), both proven to fail on the old code
>   and pass on the new. Full suite green: 484 Python + 95 render.
> - **P0-3 — MECHANISM IN REPO; Pi step pending.** `BIND_HOST` (default
>   `127.0.0.1`) is wired through `deploy/pifinance.service`, the dev
>   `app.run` in `app.py`, and `.env.example`. **Remaining, Pi-side:** set
>   `BIND_HOST=<tailnet-IP>` in the Pi's `.env`, deploy, and verify with
>   `ss -ltnp | grep 8080`. Deploying without the `.env` entry drops the app to
>   loopback (phones lose access), so it's a coordinated step.
> - **P0-2 — RUNBOOK DELIVERED; Tailscale action pending.** Chosen fix: a
>   Tailscale ACL restricting `tcp:8765` to Alta's devices (zero code, no client
>   change). Full steps + safety rails in `deploy/mcp-tailnet-acl.md`. This is an
>   admin-console action only the owner can perform.

### 1. Stored XSS: `esc()` does not escape quotes

`static/render.js:27` escapes `&`, `<`, `>` and nothing else. Five sites
interpolate `esc()`-ed **user-controlled** text into double-quoted attributes:

| Site | Value |
|---|---|
| `static/render.js:380` `aria-label="Edit ${esc(b.category)} budget"` | budget category |
| `static/render.js:381` `aria-label="Remove ${esc(b.category)} budget"` | budget category |
| `static/render.js:842` `aria-label="${esc(it.name)} is ${it.status}…"` | pantry item name |
| `static/render.js:844` `aria-label="Set a purchase match for ${esc(it.name)}"` | pantry item name |
| `static/render.js:846` `aria-label="Stop tracking ${esc(it.name)}"` | pantry item name |

**Verified by execution**, not inspection. Running the real shipped
`inventoryHTML` with an item named `Milk" onmouseover=alert(document.cookie) x="`
produced:

```html
aria-label="Milk" onmouseover=alert(document.cookie) x=" is low; tap to change">low</button>
```

A live event handler on a real button. The backend does not stop it:
`add_item` (`actions.py:1334`) and `set_budget` (`actions.py:1421`) only
`.strip()[:100]` / `[:60]` — length, no charset restriction.

**Impact.** Items and categories are household-shared, so one member's payload
executes in the other's session. That session is fully privileged:
`SESSION_COOKIE_HTTPONLY` blocks cookie theft, but same-origin `fetch()`
inherits the session, so the payload can `POST /api/tokens` to **mint itself a
`read,write` bearer token** and exfiltrate it. Effectively account takeover.
`ledger_add_item` also lets the Ask assistant create arbitrary item names, which
chains this to finding 7.

**Worth preserving:** the team *knew* this. `static/render.js:362-364`
documents the rule — user content goes through ids and indices, never
attributes, precisely because `esc` doesn't escape quotes — and the violation is
sixteen lines below the comment. `tests/test_render.js:24-26` explicitly pins
the quote-passthrough as intended behaviour.

**Fix.** Add `.replace(/"/g,"&quot;").replace(/'/g,"&#39;")` to `esc`. Two lines
in one function protects all 81 call sites; do not patch the five sites
individually. Update the pinning test to the new contract and add an
attribute-breakout regression proven to fail first. `&quot;` renders as `"` in
text nodes and decodes correctly through `dataset`, so there is no display
regression. Frontend-only, no gate.

### 2. The MCP server has no inbound authentication

`ledger_mcp.py:565-570` checks only that `LEDGER_MCP_TOKEN` is *set* — that
token authenticates **outbound** calls to Flask. It then runs
`mcp.run(transport="streamable-http")` with no verifier, no middleware, no auth
hook.

**Verified over the tailnet:** `http://raspberrypi:8765/mcp` answers **406, not
401** — the MCP protocol layer is reachable unauthenticated. (Probe stopped
there; no tool invoked, no data read.)

Because the Pi's token was minted `read,write` (CLAUDE.md, Aug 1 deploy), every
tailnet peer that can reach `100.108.237.13:8765` can, with no credential:

- read the complete financial picture (`ledger_household_snapshot`,
  `ledger_search_transactions`);
- perform direct writes (`ledger_classify_inflow`, `ledger_set_rule_enabled`);
- drive **both halves** of the two-phase tier (`ledger_propose_income_rule` →
  `ledger_confirm_action`).

The "human approves the preview" step is a prompt instruction to a well-behaved
client, not a server-enforced gate. Charlee's phone holds a Tailscale
device-share of the Pi node, so the tailnet is the entire authentication
boundary for a write-capable API. Writes land in `audit_log` as `mcp:<label>`
— i.e. attributed to Alta.

**Fix, cheapest first:**
- Tailscale ACL restricting `tcp:8765` to Alta's devices (zero code); or
- bind `LEDGER_MCP_HOST=127.0.0.1` behind `tailscale serve` (adds TLS + identity
  headers); or
- add an inbound bearer check to the FastMCP app.

Also worth splitting: run the default instance under a `read` token so the
blast radius is read-only unless writes are explicitly requested.

### 3. gunicorn binds `0.0.0.0`

`deploy/pifinance.service:12` — `--bind 0.0.0.0:${PORT}`. The unit is
`sed`-rewritten on copy only for the `pi`→`altamash` user and path, so the bind
reaches the Pi. `app.py:1578` (dev path) does the same.

Combined with no `SESSION_COOKIE_SECURE` (`app.py:46-51`, deliberate since the
app is served over `http://`) and no rate limiting or lockout on
`POST /api/login` (`app.py:256`), any device on the home Wi-Fi can reach the
login page, sniff the plaintext credential POST and session cookie, and
brute-force an 8-character-minimum password. Tailscale encrypts the tailnet
path; it does nothing for the LAN path that is also open.

**Fix.** Make the bind host an `.env` variable (`BIND_HOST`, default
`127.0.0.1`) so the tracked unit is safe by default. **Verify current state
first** with `ss -ltnp | grep 8080` on the Pi.

---

## P1 — High

> **Remediation status (updated in-session) — 10 of 14 done, all verified, suite
> green at 493 Python + 96 render:**
> - **#4 FIXED** — `pay_bill`/`contribute` use `g.auth["user_id"]`; two bearer
>   regression tests added (proven to fail on the old code).
> - **#5 FIXED** — per-tab `window._*` stashes cleared at the top of `render()`
>   (`ROW_STASHES`), killing the stale-write-back.
> - **#6 FIXED** — the `state.month` mutation removed from `renderBills`; one
>   local `thisMonthISO()` helper now feeds both the initial month and the Bills
>   header (no more UTC/local split).
> - **#9 FIXED** — a second render build guard resolves every bare call in
>   `app.js`; catches a call to a name that exists nowhere (the real outage
>   class). Proven to bite on a typo.
> - **#10 FIXED** — the four `finance.db` guards are now case-insensitive AND
>   symlink-resolving (`basename(realpath(path)).lower()`). Proven on `Finance.db`
>   and a `dev.db -> finance.db` symlink.
> - **#11 FIXED** — `WRITE_RE` widened (INSERT OR REPLACE/IGNORE, REPLACE INTO,
>   DROP/ALTER TABLE) and the invariant-1 scan switched to full-text. Proven to
>   bite end-to-end on an injected `INSERT OR IGNORE INTO items`.
> - **#13 FIXED** — CSP (`script-src 'self'`) + `nosniff` + `Referrer-Policy` via
>   `after_request`; verified CSP-safe (no inline scripts).
> - **#14 FIXED** — `@app.errorhandler(Exception)` returns JSON for `/api/*`
>   (404 and unhandled 500), so a backend error can't blank a tab.
> - **#15 DONE (Pi-activated)** — service unit bumped to `--workers 4 --timeout
>   120`; takes effect on the next deploy.
> - **#17 FIXED (timing half)** — login always runs a hash comparison against
>   `_DUMMY_PW_HASH`, closing the username-enumeration oracle. Login route now has
>   tests (also starts closing #21). **Rate-limiting half still open** (needs a
>   storage decision — in-process is per-worker with 4 workers).
> - **#8 FIXED** — the tripwire fixture now seeds `items` and the probe adds
>   three matching in-window shopping-category inflows, so removing the repo's
>   `direction='out'` filters trips **8 of 15** discovered functions (was 1),
>   proven by mutation; the test reports the full blast radius; CLAUDE.md's
>   "tripwire-covered" claim is corrected at its definitional point.
> - **#12 FIXED** — the gate snapshot gained optional `by_category` + `income`
>   sections (compared only when both sides have them, so the v1.0-baseline path
>   is preserved). A test proves a category reassignment that leaves the monthly
>   total unchanged is now caught; another proves the legacy baseline still gates
>   clean.
> - **#16 FIXED (hottest path)** — `txn_to_json`/`payer_share_pct` gained a
>   batched `prefetch_payer_shares`; `/api/transactions`, `/api/activity`, and
>   dashboard-recent now issue **one** splits query instead of one per row
>   (measured 243→1). Byte-parity preserved (`test_api_parity` green).
>   *Remaining sub-optimizations (secondary, lower-frequency):* the pantry
>   `_matching_purchases` per-item scans (a shared `_purchase_index`) and
>   `_monthly_series`'s per-month `spending_summary` — left as smaller follow-ups.
> - **STILL OPEN:** #7 (prompt-injection delimiters + refund confirm), the
>   rate-limiting half of #17, and the two secondary #16 sub-paths above.

### 4. Live bug: `pay_bill` and `contribute` 500 under a bearer token

`app.py:1399` and `app.py:1488` read `session["user_id"]` directly, but both are
decorated `@login_required`, which accepts **session OR bearer**
(`app.py:123-137`). With a token there is no session, so the dict access raises
`KeyError` → HTTP 500.

Reproduced against a seeded database with a real `read,write` token:

```
POST /api/bills/<id>/pay        (write bearer): KeyError: 'user_id'
POST /api/goals/<id>/contribute (write bearer): KeyError: 'user_id'
POST /api/transactions          (write bearer, control): 201
```

Every other authenticated route uses `g.auth["user_id"]` (`app.py:284, 354,
374, 389`). These two are the only outliers. Not reachable from the shipped MCP
tool surface today, but `read,write` tokens are live on the Pi and the Ask
loop's in-process caller uses these same routes.

**Why nothing caught it:** `POST` and `DELETE /api/bills/<id>/pay` have **zero
tests of any kind**, and `/contribute` is tested only through a session.

**Fix.** Use `g.auth["user_id"]` in both, and add bearer-token tests.

### 5. Stale cache silently reverts an edited transaction

`window._txns` is written at `static/app.js:444` (`renderActivity`) and **never
cleared**. `findTxn` (`static/app.js:727`) searches it first.

Sequence: edit a transaction on Activity → navigate to Dashboard → tap the same
row. The dialog pre-fills the **pre-edit** values and Save writes them back.

This is the only finding in the review that can silently corrupt money data, and
it lives entirely in the frontend where the balance gate cannot see it.

**Fix.** Fold the eight `window._*` stashes into `state.rows`, cleared at the
top of `render()`.

### 6. Visiting Bills silently resets the viewed month

`static/app.js:491` performs an assignment **inside a template expression inside
a heading**:

```js
`<p class="eyebrow" …>Bills — ${monthName(state.month = new Date().toISOString().slice(0,7))}</p>`
```

Navigating to Bills discards the month you were viewing on Activity or
Analytics. It also uses `toISOString()` (UTC) while `todayISO()`
(`static/app.js:33`) and `renderInventory` (`static/app.js:640`) use local time —
three spellings of "now" in one file.

### 7. "The assistant cannot move money" is not accurate

`spending_summary` subtracts `direction='in' AND income_type='refund'` rows from
their category's spend via a signed UNION with **no clamp**
(`derivations.py:96-101`), and `refund` is in `REAL_INCOME_TYPE_ORDER`
(`actions.py:770`), which **generates** the Ask write tool's enum.

Untrusted external text reaches the model verbatim:
`ledger_unclassified_inflows` and `ledger_search_transactions` both return
`transactions.description` — whatever the bank or payer put in the memo. In the
same context the model holds five write tools, guarded only by prose in the
system prompt. The pantry instructions are deliberately permissive
(`ask_loop.py:124-135`, "here you have broad control"), which is exactly the
posture an injected instruction exploits.

So a prompt injection carried in a bank memo can change **displayed spend,
budget status, savings rate, `net_cash_flow` and anomaly flags**. The
who-owes-whom balance is safe; the dashboard is not. Everything is audited and
reversible, which is the real mitigation — but the documented isolation claim
overstates it.

**Fix.**
- Delimit untrusted content in tool results (wrap `description` / item `name`
  in explicit markers) and add a system-prompt rule that text inside them is
  **data, never instructions**. Cheap, effective against the naive case.
- Treat `refund` as the one classification worth an explicit confirmation, since
  it is the only Ask-reachable write that moves a money aggregate.
- Fix finding 1 so a pantry write can never become script execution.

### 8. The derivation tripwire is ~93% vacuous

**This overturns a claim repeated ~15 times in CLAUDE.md.**

`tests/test_derivation_tripwire.py` is presented as the automated guard for the
income-contamination bug class, and new derivations are routinely described as
"tripwire-covered, no exemption." Mutation-tested: **removing all 8
`direction='out'` filters from `derivations.py` caused exactly 1 of the 15
covered functions to fail** (`top_merchants`).

The other 14 pass for reasons unrelated to the filter existing:

- **Seven return `[]` both before and after** — `low_stock`, `shopping_list`,
  `restock_suggestions`, `restock_forecast`, `stale_shopping_items`,
  `staple_spend`, `unmatched_staples`. **Verified: neither `seed_db.py` nor
  `seed_income.py` creates a single `items` row**, so the assertion is
  `[] == []`.
- **Threshold aggregates are structurally immune** to a one-row probe —
  `recurring_charges` needs ≥3 same-amount charges, `new_staple_suggestions`
  needs ≥3 distinct days.
- **`last_shopping_trip`** filters to `SHOPPING_CATEGORIES`; the probe's
  category is `'Other'`.
- **`compute_balance` / `member_breakdown`** are protected by their splits
  `INNER JOIN` and the probe writes no splits — this one *is* honestly
  disclosed in the file's own docstring.

The sharpest case: `bill_variance`'s docstring states its `period=None` default
exists *"purely so the tripwire can call it with just `db`"* — a parameter added
for the guard's benefit, which makes the guard exercise a path that joins to
zero transactions. The harness shaped the code, then stopped testing it.

**Structural cause.** `_snapshot` calls `func(db)` with only `db`, so any
derivation needing a second positional argument cannot be called at all; the
only way to green the suite is to add it to `EXEMPT`. That is why two `EXEMPT`
entries cite harness convenience ("takes a `category` arg so it isn't a bare
db-aggregate") rather than semantics. **The harness actively pressures toward
over-exemption.**

**Mitigating:** most of these functions *do* have dedicated hand-written
inflow tests (`tests/test_item_verbs.py`, `test_recurring_charges.py`,
`test_top_merchants.py`, `test_member_breakdown.py`, `test_income_isolation.py`).
**Those are the real coverage. The tripwire contributes almost nothing.**

**Fix.** Either seed `items` rows and give the probe multiple
same-merchant/same-amount rows in a shopping category dated **inside** the
fixture window (it currently inserts at `date.today()`, drifting further outside
the 2026-05→07 fixture every day) — or accept the hand-written tests as the real
coverage and **correct the "tripwire-covered" language in CLAUDE.md**. The
belief that new aggregates are covered for free is what would let the next
contamination bug through.

### 9. The post-outage render guard does not close the outage class

`tests/test_render.js:800-813` was added after the Analytics tab blanked
("Can't find variable: budgetStatusHTML"). Mutation-tested three ways:

| Scenario | Result |
|---|---|
| Helper exists, called in app.js, removed from destructuring (**the original outage**) | ✅ caught |
| Helper renamed in render.js, app.js calls the old name | ⚠️ caught, but by the helper's own unit test, not the guard |
| app.js calls a name that **exists nowhere** (typo / deleted helper) | ❌ **missed** — renaming 3 `catEmoji(` call sites to `catEmojiX(` still printed `render tests passed (94 checks)` |

The guard iterates `Object.keys(R)` — what render.js *exports* — and asks
whether app.js calls them. It never asks the inverse: *does every bare call in
app.js resolve to something?* That inverse is the actual runtime failure mode.
The rename case only fails today because 36 of 37 exports happen to have a unit
test.

**Operationally important:** `node` is not installed on the Pi, so
`tests/test_frontend_render.py:17-19` **skips the entire 94-check seam there**.
"The suite is green" in the deploy environment excludes all frontend
verification.

**Fix.** Parse app.js's bare call sites and assert each resolves to a local
definition or the destructuring block. Separately, replace
`const { … } = window.Render` with `const R = window.Render` + a presence check
— that makes the whole outage class structurally impossible, and also removes a
latent `SyntaxError` risk: if `txnRow`/`beamHTML` are ever moved to render.js
while left in app.js (which CLAUDE.md plans), `const { txnRow }` plus
`function txnRow` in one script scope means app.js never parses and the entire
app goes blank.

### 10. Two safety gaps around `finance.db` itself

Given hard rule 6, these outweigh their size.

- **The name guard is a case-sensitive basename match** —
  `os.path.basename(path) == "finance.db"` in `seed_db.py:236`,
  `seed_income.py:117`, `migrate.py:39`, `gate.py:39`. macOS is
  case-insensitive (verified), so `migrate.py apply Finance.db` **passes the
  guard and opens the live file**. A symlink named `dev.db` would too.
  One-line fix: `.lower()` in all four.
- **`os.environ["DATABASE_PATH"]` is set by 40 test files and restored by
  none**, while `app.py:33` defaults to `finance.db` with **no name guard**.
  Safe today only because every file happens to set it before loading the app —
  a convention, not an enforced invariant. On the Pi, one future test file that
  forgets would open the live database.

### 11. The invariant-1 guard has proven bypasses

`WRITE_RE` (`ontology.py:43-44`) is
`\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)`. Evaluated against four
write forms:

1. **Multi-line SQL** — `test_no_raw_writes_to_governed_tables_outside_verbs`
   (`tests/test_architecture.py:72`) iterates `splitlines()`, so
   `INSERT INTO\n    transactions` escapes. `actions.py` formats SQL as
   triple-quoted multi-line blocks, so this is house style, not an exotic edge
   case. (The sibling staleness test scans full text — the two disagree on what
   a write is.)
2. **`INSERT OR REPLACE INTO` / `INSERT OR IGNORE INTO`** — valid SQLite, not
   matched.
3. **`REPLACE INTO`** — valid SQLite, not matched.
4. **`CREATE` / `DROP` / `ALTER TABLE`** — invariant 7 is not covered by this
   scan at all.

No live violations today. This is guard strength, not a live bug.

Related: `ALLOWED_PATHS` (`tests/test_architecture.py:35`) matches on
`rel.name`, so *any* file anywhere named `actions.py` is exempt.
`_defined_verbs()` (`:110`) matches `^def NAME(db, actor` — a verb written
`def foo(db,actor` or `def foo(db, *, actor)` is invisible — while
`ontology._verbs()` does the same job properly via `inspect.signature`. Two
discovery rules for one concept, in a file that preaches single-sourcing.
`_registry_verbs()` (`:117-120`) accepts any backticked identifier on any
markdown table row, and checks one direction only, so a stale registry row for a
deleted verb never fails.

**Fix.** Widen to
`\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE)\s+["'\[]?([a-z_]+)`,
scan full file text, and point `test_architecture` at `ontology._verbs()`.
Verify each widened tooth bites by temporarily introducing the bypass form.

### 12. The balance gate is narrower than its reputation

`gate.py:79-97` snapshots exactly three things: the net balance, one grand
`total_cents` per month, and per-table row counts. **Not** captured:
per-category totals, per-member paid/owed, `income_summary`
(`true_income`, `gross_inflows`, `savings_rate`), `budget_status`,
`bill_variance`, goal totals.

So "GATE PASS — no money moved" does not cover category reassignment or income
misclassification. And once the household exceeds two members (which invariant 5
anticipates), it will not catch offsetting split changes that preserve the net.

**Fix.** Add an *optional* `income` + `by_category` section to `snapshot`,
emitted only when the ref's `derivations` exposes `income_summary` and compared
only when both sides have it — preserving the v1.0-baseline path while covering
everything since.

### 13. No CSP or security headers

Verified: no `@app.errorhandler`, no `after_request`, no
`Content-Security-Policy`, no `X-Frame-Options`, no `nosniff`, no
`Referrer-Policy` — in `app.py` or `static/index.html`.

CLAUDE.md cites "CSP + no build step" as the reason for hand-rolled SVG charts,
which suggests the team believes one is in place. It is not. The app is an ideal
candidate: same-origin scripts only, no inline `<script>`, no CDN. A strict
`script-src 'self'` would have neutralised finding 1 entirely.

**Fix.** One `after_request`:
`default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`
plus `nosniff` and `Referrer-Policy: same-origin`. Inline `style="width:…"` is
used throughout `render.js`, so `style-src` needs `'unsafe-inline'` — that's
fine; `script-src` is the load-bearing one.

### 14. No JSON error handler

There is no `@app.errorhandler` anywhere. Any unhandled exception returns
Flask's HTML 500 page; `api()` (`static/app.js:46-59`) fails to parse it, and
`render()`'s single outer `try` blanks the whole tab with one line of grey text
— **exactly the `budgetStatusHTML` outage symptom**.

**Fix.** `@app.errorhandler(Exception)` → `jsonify` + `app.logger.exception`,
plus `@app.errorhandler(404)` for JSON on `/api/*`. Pair with
`Promise.allSettled` on the frontend so one dead card doesn't kill twelve live
ones.

### 15. Capacity: 2 workers, no timeout, and `/api/ask` holds one

`deploy/pifinance.service:12` — `--workers 2`, sync workers, no `--threads`, no
`--timeout` override (default 30s).

- `renderAnalytics` fires **13 parallel fetches** against 2 concurrent slots;
  requests queue 7 deep.
- **`POST /api/ask` holds a worker for the entire Anthropic loop** — up to 6
  model round-trips. One Ask query occupies 50% of app capacity; two concurrent
  (Alta + Charlee) block the app, and the 30s default will kill a slow one
  mid-flight.

**Fix.** `--workers 4 --timeout 120` (the Pi 5 has 4 cores), and/or collapse the
analytics fan-out into one `/api/analytics/overview` composite — which also lets
every card share a single `spending_summary` computation.

### 16. N+1 in the two hottest read paths

- **`txn_to_json`** (`app.py:170-181`) calls `payer_share_pct`
  (`actions.py:135-146`), which issues **one `SELECT` per transaction**.
  `/api/transactions` (`LIMIT 500`) → up to **501 queries**; `/api/activity` →
  502; `/api/dashboard` → 7.
- **`_monthly_series`** (`derivations.py:196`) calls `metric_fn(db, month)` per
  month, so `income_trend(months_back=6)` runs `spending_summary` 6× — but
  `spending_summary(db)` with `month=None` **already returns every month in a
  single `GROUP BY`**. The trend engine can be O(1) queries instead of
  O(months).
- **The pantry is worst.** `_matching_purchases` (`derivations.py:689`) does an
  unindexable `instr(lower(description), lower(?))` **per item**, and
  `GET /api/inventory` calls six derivations that each loop over staples. With
  30 staples that is ~150 full table scans per pantry load.

**Fix.** Batch the splits lookup into one `WHERE transaction_id IN (…)` dict —
output is byte-identical and `tests/test_api_parity.py` proves it. Hoist one
outflow scan into a shared `_purchase_index(db)` for the pantry. The correct
batched pattern already exists in your own `search_transactions_view`.

### 17. No rate limiting; login timing oracle

`POST /api/ask` is well-bounded **per request** (`max_rounds=6`,
`max_tokens=1024`, `MAX_CONTENT_LENGTH=64KB` — a genuinely good set of
ceilings) but unbounded **per unit time**. Any session can burn the Anthropic
balance; each request also spawns two `app.test_client()` instances and up to 6
sub-requests, so it's a compute amplifier on a Pi.

`POST /api/login` (`app.py:262-266`) returns early when the username is unknown,
skipping `check_password_hash` — a measurable enumeration oracle, and no
lockout. Matters mainly in combination with finding 3.

**Fix.** One small counter keyed on `session["user_id"]` / username serving
both (note `--workers 2` means an in-process dict is per-worker). Compare
against a dummy hash when the row is `None`.

---

## P2 — Medium

### 18. Single-sourcing stopped at the Python boundary

The action-schema work closed the two *agent* doors. The vocabulary source is
`actions.py:770 REAL_INCOME_TYPE_ORDER`. Still hand-copied, unbound by any test:

| Copy | Note |
|---|---|
| `static/app.js:955 INCOME_TYPES_UI` | **The picker Charlee actually taps** |
| `agent_read_tools.py:270-272` | Literal enum in the *shared* read registry; the module never imports `actions` |
| `ledger_mcp.py:366` | Full list as prose on `search_transactions` |

Add a seventh income type and the verb, Ask and MCP-write all update — while the
app's own tagging dialog silently goes stale. Same shape for item statuses:
`actions.py:1320 ITEM_STATUS_ORDER` vs `derivations.py:658 _ITEM_STATUS_ORDER`
vs `static/app.js:633 NEXT_STATUS`.

**Fix.** `GET /api/vocabulary` returning `{income_types, item_statuses,
item_kinds}` built from the existing `*_ORDER` tuples, fetched once beside
`loadCategories()`; and make `agent_read_tools` import `actions` like everything
else.

### 19. Seven assertions that cannot fail

Each reduces to `x == x` because test and production evaluate the same
expression:

| Location | Why it can't fail |
|---|---|
| `tests/test_action_schema.py:45` | `actions.py:1319` defines `ITEM_KINDS = frozenset(ITEM_KIND_ORDER)` |
| `tests/test_action_schema.py:46` | same construction |
| `tests/test_action_schema.py:47-48` | `actions.py:772` derives `INCOME_TYPES` from the tuple |
| `tests/test_ontology.py:86-91` | `ontology.py:183` builds `params` by calling `actions.param_schema(name)` |
| `tests/test_ontology.py:122-126` | `ontology.py:198-200` are `list(actions.REAL_INCOME_TYPE_ORDER)` etc. |
| `tests/test_ontology.py:128-130` | `ontology.py:193` assigns from the same import |
| `tests/test_action_schema.py:32-34` | `agent_write_tools.py` *generates* those schemas by calling `param_schema` |

Both files' docstrings claim the opposite — `tests/test_ontology.py:6-8` says
each check is *"cross-derived a DIFFERENT way… so the test and the code can't
share a bug"* (true for one test in that file, false for four). These were
load-bearing and were quietly obsoleted by the very refactor they guarded;
nobody removed them, so they now **report coverage that does not exist**.

And the drift the whole design exists to prevent is still open: `VERB_TOOL`
(`tests/test_action_schema.py:17-23`) is hand-maintained and nothing asserts it
covers all of `PARAM_SPECS` or all of `WRITE_TOOLS`. A sixth write tool with a
hand-written schema passes every test in the file.

**Fix.** Delete or rebuild the seven, correct the two docstrings, and add
`assertEqual(set(VERB_TOOL), set(actions.PARAM_SPECS))`.

### 20. `action_transaction` has no direct test

`actions.py:59` is CORE-DESIGN's transaction-boundary contract: `BEGIN
IMMEDIATE`, rollback on any failure, refuse a pre-open transaction. **No test
anywhere calls it or asserts rollback.** Only
`tests/test_record_transaction.py:229` tests atomicity, and only for one verb.
Remove the rollback and partial writes (row without splits, edit without audit)
become possible with a green suite.

Also untested directly: `to_cents` and `parse_share_bp` — the two functions
enforcing hard rule 3 — have no boundary tests (`0.005`, `1e400`, `"abc"`,
`100.001`).

### 21. Routes with no test at all

| Route | Note |
|---|---|
| `POST /api/setup` (`app.py:222`) | The only account-creation path — **and the one `KNOWN_EXCEPTIONS` invariant-1 waiver**. Password-length, duplicate-username and already-completed-403 all unexecuted. |
| `POST /api/login` (`app.py:256`) | **The only password-verification code in the app.** All 55 authentications inject `session["user_id"]` directly. |
| `POST /api/logout` (`app.py:272`) | Session clearing untested. |
| `POST /api/bills/<id>/pay` (`app.py:1389`) | Zero references. Contains finding 4. |
| `DELETE /api/bills/<id>/pay` (`app.py:1407`) | Zero references. |
| `POST /api/tokens/<id>/revoke` (`app.py:382`) | Verb tested; route's `AND user_id = ?` **ownership scoping** — a security control — is not. |

**The least-tested code is exactly where the constitution is suspended.**

### 22. Order-dependent test

`tests/test_goal_routes.py:17-38` uses `setUpClass`; two tests share one mutable
database with no reset. Run in reverse method order it fails: one test deletes a
goal, SQLite reuses the rowid, and the assertion at `:99` queries
`audit_log WHERE target = 'goal:<id>'` and picks up the previous test's
`delete_goal` row. It passes only because alphabetical ordering happens to be
favourable.

Also: **22 test files depend on the real clock**, against a project convention
that deliberately froze fixtures at `--as-of 2026-07-19`.

### 23. Timezone mixing between `_now()` (UTC) and `txn_date` (local)

`actions.py:78 _now()` → UTC. `simplefin_sync.py:172` → local calendar date.
`app.py:245` → local. `actions.py:92 current_period()` → local.

Then `derivations.py:746` (`restock_suggestions`) and `derivations.py:949`
(`stale_shopping_items`) compare `updated_at[:10]` against `txn_date >= ?`. In
US Eastern, an item marked low after 19:00 gets a UTC `updated_at` dated
*tomorrow*, so that evening's purchase is silently skipped for a day.

**Fix.** One `_today_local()` / `_now_local()` pair for anything compared to
`txn_date`, or normalize `txn_date` at ingest. Pick one and state it in
CORE-DESIGN.

### 24. Non-atomic compound writes

- `app.py:717-724 update_inventory_item` calls `set_item_status` then
  `set_item_match` — two `action_transaction`s in one request. If the second
  fails, the first is committed and the response is a 4xx describing a
  partially-applied edit.
- `actions.py:1211-1228 confirm_action` — honestly documented, but
  `create_income_rule` and `_apply_single_rule` are two commits, so a crash
  between them leaves a rule created but unapplied and the effect no longer
  equals the previewed count.

**Fix.** An internal `_locked` form of each verb (validate + edit + audit, no
transaction of its own) that the public verb wraps and the compound caller
composes inside **one** `action_transaction`.

### 25. Type confusion returns 500 instead of 400

`(data.get("name") or "").strip()` raises `AttributeError` on a non-string,
which no route catches (`ActionError` subclasses `ValueError`; `AttributeError`
does not). Confirmed on `POST /api/inventory`, `/api/transactions`, `/api/bills`,
`/api/goals`, `/api/actions/propose`, `PUT /api/inventory/<id>`, and
`GET /api/ops/audit?limit=abc`.

Not a breach (`debug=False`), but it fires from the agent tools too — a model
passing an integer id turns a recoverable tool error into a 500. Contrast
`/api/transactions/search`, which handles this correctly.

**Fix.** This is exactly ACTION-SCHEMA-DESIGN inc 4 (verb-side structural
validation from `PARAM_SPECS`) — fold it in there. The parity-pinned error
strings must stay byte-identical, so a type guard raises `ActionError` with a
*new* message.

### 26. The tripwire's `EXEMPT` set has a growth vector

8 of 23 public derivations are exempt (35%), but 5 are exempt for **one**
transitive reason (they read `spending_summary`). Of those,
`category_trend`, `anomaly_flags`, `budget_status` and `savings_rate_trend`
have **neither** a tripwire check **nor** a case in
`tests/test_income_isolation.py` (which covers `spending_summary`,
`compute_balance`, `member_breakdown`, `settle_up` only). Their protection is
transitive and unenforced.

Every future spend-reading derivation will need another exemption.

**Fix.** Split the tripwire into two checks — the existing "adding *any* inflow
must not move this," plus a narrower "adding a **non-refund** inflow must not
move this" that the 5 transitively-exempt functions can pass. That shrinks the
hand-maintained list from 8 to 3 genuine income aggregates.

`spending_summary`'s own exemption is the **model for how this should be done**:
`tests/test_income_isolation.py:153-170` iterates `INCOME_TYPES - {"refund"}`
rather than a hardcoded list, so a new income type is covered automatically.

### 27. `test_income_visibility_policy.py` pins less than it claims

Genuinely load-bearing (distinct amounts per member; both session and bearer
doors). But the docstring claims *"The day someone adds `WHERE paid_by =
<viewer>` … to an income surface, this fails."* It pins exactly **two**
surfaces: `/api/income/summary` and `/api/activity`. Owner-scoping added to
`/api/transactions/search`, `/api/household_snapshot`, `/api/income/trend` or
any `/api/analytics/*` endpoint would pass silently. The bearer-door test covers
only `/api/income/summary`.

### 28. Unbounded dependency pins on a deploy that runs `pip install`

`deploy/deploy.sh:146` runs `pip install -q -r requirements.txt` **after** the
gate passes. Only `mcp>=1.2,<2` is upper-bounded — the one that already bit you.
`flask>=3.0` and `anthropic>=0.40` are open-ended, and `run_ask`
(`ask_loop.py:67-86`) depends on the SDK's `resp.content` block shapes. A Flask
4.0 or Anthropic 1.0 release installs itself during a deploy the gate cannot
see.

**Fix.** Apply the mcp lesson: `flask>=3.0,<4`, `anthropic>=0.40,<1`, pin
`werkzeug` (transitive, `generate_password_hash`). Better: a
`requirements.lock` plus a post-install `python -c "import app"` smoke check
before restarting.

### 29. `.claude/worktrees/` is excluded only machine-locally

`git check-ignore -v` resolves it to `.git/info/exclude:7`, which is **not
committed and does not survive a fresh clone**. The directory holds a complete
1.2M second copy of the repo. On another machine it would appear untracked and
could be swept into a commit — plausibly with a `dev.db` or `.env` created
inside it. One line in the tracked `.gitignore` makes hard rule 7 portable.

### 30. `confirm_action` doesn't bind to the proposer; pending tokens in plaintext

The two-phase choreography is otherwise excellent (see praise below). Two
residual gaps: `created_by` is *recorded* (`app.py:615`) but never *checked*, so
any authenticated caller holding the token can confirm another identity's
proposal; and unlike `api_tokens` (SHA-256-hashed, correct),
`pending_actions.token` is stored in plaintext. Both low-impact given the
10-minute TTL and single use.

### 31. Six MCP read tools have no passthrough assertion

`tests/test_ledger_mcp.py:102-109` uses subset (`<=`) and its `READ_TOOLS`
literal omits `ledger_budget_status`. `ledger_spending_composition`,
`_savings_rate_trend`, `_member_breakdown`, `_bill_variance`,
`_list_income_rules` and `_budget_status` each have a hand-written URL/param map
in `ledger_mcp.py` that **no test executes** — a typo there fails silently. The
params currently match `app.py`; the gap is that nothing would catch a change.

Related loose assertions with no non-empty guard: `test_ledger_mcp.py:141`,
`test_item_verbs.py:348`, `test_gate.py:82-83` (this one guards the balance gate
itself). The correct pattern is used elsewhere in the same suite —
`test_activity_route.py:94` — so this is inconsistency, not house style.

---

## P3 — Smaller but real

- **Minus glyph mismatch.** `render.js:21` returns U+2212 while
  `app.py:87 money_display` uses ASCII `-`, despite `render.js:14` claiming they
  match. The Ask bot quotes server `display` strings verbatim next to
  `fmt`-rendered numbers, so one screen shows two different minus signs.
- **Three money formatters in Python** — `app.py:80 dollars()` (float),
  `app.py:87 money_display()`, and `app.py:204-205` building a balance message
  with a third spelling. `{cents, display}` is spelled inline 4× (`app.py:670,
  674, 679, 745`) despite `app.py:90 money()` existing.
- **Month validation copy-pasted verbatim 3×** (`app.py:814-823, 851-860,
  884-893`) while `spending_composition_view`, `budget_status_view` and
  `anomalies_view` validate `month` **not at all**. Extract one
  `parse_month_arg`.
- **Category vocabulary unbound across three files** — `app.py:53
  DEFAULT_CATEGORIES`, `derivations.py:1010 SHOPPING_CATEGORIES`, and
  `migrations/001:32 bills.category DEFAULT 'Bills'` (a category not in
  `DEFAULT_CATEGORIES`, so bill spend lands in a bucket the budget picker can't
  offer). Rename "Household" and `last_shopping_trip` silently returns `None`
  forever.
- **Goal `saved` computed in three places** (`app.py:1425`, `app.py:1149`,
  `derivations.py:385`) with no named derivation — invariant 6 says every
  surface calls the same function; this one has no function. Extract
  `derivations.goal_totals(db)`.
- **`actions.py:182`** launders basis points through a float
  (`parse_share_bp(...) / 100` → `50.0`, re-parsed as `Decimal(str(50.0))`). It
  round-trips only because Python's `repr` is shortest-roundtrip. Return
  `share_bp` as an int; convert at the JSON edge.
- **`actions.py:298`** — the dedupe no-op returns *after* `BEGIN IMMEDIATE`, so
  every already-seen row in sync's overlapping lookback takes the exclusive
  write lock. Check `external_id` with a `SELECT` first.
- **`app.py:123-137 login_required`** infers bearer scope from `request.method`.
  No GET route calls a verb today, so it's correct — but it **fails open** the
  day someone adds a GET that writes. Annotate routes explicitly
  (`@writes("verb")`) and assert coverage in `test_architecture`.
- **`actions.py:1299-1311 find_active_api_token`** commits a `last_used_at`
  write on *every* authenticated bearer request, including reads.
- **`derivations.py:7 round_ratio`** docstring says "positive rational" but the
  implementation is correctly banker's-rounded for negatives — which the refund
  dip depends on. No test pins that; a future "simplification" would silently
  break refund-month rounding.
- **`app.py:1212`** — `LIKE '%'||lower(?)||'%'` doesn't escape `%`/`_`.
  Parameterized, so no injection; just surprising matches.
- **`static/app.js:408/438`** — `LIMIT 500` truncates with no `has_more`, unlike
  the search endpoint.
- **11 write paths have no `.catch()`** (`static/app.js:604, 610, 619, 644, 652,
  664, 674, 782, 809, 1104, 173`) — a 400 vanishes as an unhandled rejection
  with zero user feedback. Dialog-based writes handle this correctly; inline
  ones don't. Nothing is ever `console.error`d anywhere in the SPA.
- **GitHub PAT on the curl command line** (`deploy/ops-health-check.sh:260-265`)
  — visible in `/proc/<pid>/cmdline`. Use `curl -K` with a mode-600 file.
- **SimpleFIN robustness** — `simplefin_sync.py:173` will raise on a malformed
  feed value and abort the entire sync run. Availability-only (per-call atomic),
  but one bad upstream row silently stops bank data flowing. Wrap the
  per-transaction parse and skip.
- **`--claim <setup-token>`** puts a single-use token in argv and shell history.
- **Stale artifacts** — `dev-old.db` (Jul 18) and `seed.db` in the working tree.
  Gitignored, so harmless, but cruft.
- **Dead code** — `tests/test_reset_money.py:107-109` loads `app.py` into `mod`
  and never uses it. `tests/test_simplefin_sync.py:87-88`'s comment credits a
  mechanism that doesn't exist ("FakeRequests would raise if hit" — it returns a
  response and never raises); the row-count check at `:95` is the real teeth.

---

## File size and cohesion

The god-module framing is half right. By **code lines** excluding
docstrings/comments/blanks:

| File | Total | Code | Docstring share |
|---|---|---|---|
| `actions.py` | 1608 | ~852 | 32% |
| `derivations.py` | 1076 | ~452 | **43%** |
| `app.py` | 1579 | **~1101** | 15% |

`derivations.py` is **not** a god module — it's 452 lines of small pure
functions with exceptional documentation. **`app.py` is the actual problem**:
the most code, the least explanation, and 60 routes with no internal boundary.
There are no god-*functions* anywhere (longest: `record_transaction` 83 lines,
`settle_up` 81, both including large docstrings).

**Do `app.py` → blueprints first.** It's the biggest win and you already own the
safety net that proves it: `tests/test_api_parity.py` pins byte-identical
responses against the v1.0 baseline.

| New module | Current lines | ~LOC |
|---|---|---|
| `web/common.py` | db helpers, money helpers, auth decorators, `parse_month_arg` (new) | 130 |
| `web/analytics.py` | 784–1118 | 334 |
| `web/pantry.py` | 644–784 | 140 |
| `web/agent.py` | 287–396, 601–644, 1118–1305 | 300 |
| `web/money.py` | 396–530, 1321–1543 | 350 |
| `app.py` | factory, config, `index()` | 90 |

Watch `_VERSIONED_ASSETS` (`app.py:1547`), which is hardcoded and mirrored in
`tests/test_index_cache_busting.py` — derive it by regexing the shell instead.

Then `actions.py` → package (`_kernel`, `money`, `income`, `pantry`, `budgets`,
`agent`, `schema`, `admin`) with `__init__.py` re-exporting.
**⚠️ This breaks two guards in the same commit:** `tests/test_architecture.py:35`
allow-lists the basename `"actions.py"` and `:109` does
`(REPO/"actions.py").read_text()`. Both must move to a directory allow-list +
`ontology._verbs()` simultaneously. `ontology.py:52` filters on `fn.__module__`,
so it needs care too.

`derivations.py` → package is **safe** for the tripwire —
`tests/test_derivation_tripwire.py:76` uses `inspect.getmembers`, which sees
re-exported names. The seam is already marked by the comment banner at line 655.

Both JS files are at the wall (1206 / 1023 lines). A no-build-step split via
`Object.assign(root.Render || {}, api)` in the UMD tail is straightforward.

---

## What is genuinely excellent

Specific, not flattery — these are the things that held up under adversarial
reading:

- **One write path holds.** Not a single raw `INSERT`/`UPDATE`/`DELETE` against
  a governed table exists outside `actions.py`, migrations and fixtures — and
  `test_known_exceptions_are_still_real` forces each waiver to still correspond
  to a live write, so an exception can't rot into a mask. That counter-test is a
  level of care most codebases never reach.
- **Bearer tokens are textbook.** SHA-256-hash-only storage,
  `secrets.token_urlsafe(32)`, plaintext returned exactly once and never
  re-derivable, per-person, revocable, `revoked` checked in the lookup query
  itself, and the audit row deliberately records label/user/scopes but **never**
  the token or its hash.
- **`ontology._transitive_writes()`** derives verb→table attribution by
  expanding `actions.py`'s internal call graph, cycle-safe with a memo, so a
  verb that delegates to `delete_transaction_graph` or `_write_audit` is charged
  with its helpers' writes. Self-description by reflection, not a hand-kept map.
- **The two-phase claim ordering is correct, and the comment explains why** —
  including the honest observation that a crash between claim and dispatch drops
  the write rather than repeating it, and that dropping is the safe direction.
  I could not find a replay path.
- **`reset_money` is ACL-by-omission done right** — no route at all, an exact
  confirm phrase, CLI-only, and `tests/test_reset_money.py:111` asserts its name
  never appears in `app.py`.
- **`session_required` on `/api/ask` with the reason inline** — "a read token
  must never trigger paid API calls" — a security decision that survives because
  it's documented at the point of enforcement.
- **`round_ratio`** is integer division with explicit banker's rounding via
  `divmod`, and correct for negative numerators (the refund-dip case) despite
  the docstring only promising positives.
- **The clock-free derivation discipline** — `restock_forecast`,
  `unmatched_staples`, `stale_shopping_items`, `last_shopping_trip` return only
  history-derived facts and push "today" to the view layer. That's *why* they
  need no frozen clock, and it keeps them inflow-insensitive.
- **Docstrings that record decisions, not mechanics** — why the refund dip isn't
  clamped, why `direction` is a verb parameter rather than read from `data`
  (closing a real attack path), why `top_merchants` is deliberately *not*
  refund-netted. 42% of `derivations.py` is reasoning. That is not bloat.
- **Test isolation is right where it counts:** no test does a module-level
  `import app`; all 40 files load `app.py` through
  `importlib.util.spec_from_file_location` *after* setting `DATABASE_PATH`. That
  ordering is what keeps the suite off the live database.
- **`test_two_phase_actions.py` is the strongest file in the suite** — it
  asserts what *didn't* happen at phase 1 (0 rules, still unclassified, 0 audit
  rows) and proves scoping with a second rule that must not fire.
- **`test_income_isolation.py`** deliberately manufactures
  invariant-violating fixtures (inflows *with* split rows, which
  `record_transaction` never produces) to prove the explicit filters do the
  protecting, not incidental absence.
- **Secret hygiene in `simplefin_sync.py`** — mode-600 `os.open`, never printed,
  and the exception handler prints only `e.__class__.__name__` so a
  credential-bearing URL can't reach journald.
- **The Ops guardian is read-only by construction**, never opens `finance.db`,
  and integrity-checks backups with `immutable=1`. The backup prune refuses to
  delete anything until the *new* backup passes `PRAGMA integrity_check` —
  integrity before trust, correctly ordered.
- **`mcp>=1.2,<2` with the reason written next to it** — exactly the right
  response to an upstream breaking change.
- **Verified across all 68 test files:** zero no-assertion tests, zero `except`
  clauses swallowing failures, zero skip decorators, zero `pass`-bodied tests.
- **The habit of never trusting a regression test until it's been watched to
  fail once** is the single healthiest practice in the repo. It's why the guards
  that do bite, bite.

---

## Recommended order

Items 1–10 touch no verb, derivation, schema or money path — all ship through
the zero-gate path.

1. **`esc()` escapes quotes** + attribute-breakout regression *(P0, one
   function, largest risk reduction)*
2. **Tailscale ACL on `tcp:8765`**; move gunicorn off `0.0.0.0`
3. **`g.auth["user_id"]` in `pay_bill` + `contribute`** + bearer tests *(the
   only live bug)*
4. **Lowercase the four `finance.db` guards** *(one line, protects hard rule 6)*
5. `window._txns` stale-write fix; `state.month` mutation out of `renderBills`
6. **Correct CLAUDE.md's "tripwire-covered" claims**, then either seed `items`
   and date the probe inside the fixture window, or name the hand-written tests
   as the real coverage
7. Tests for `/api/login` + `/api/setup`; a direct test of `action_transaction`
   rollback
8. Give the render guard the inverse direction; replace the destructuring with
   `const R = window.Render`; get `node` onto the Pi or state that frontend
   checks are dev-only
9. `@app.errorhandler` JSON + `Promise.allSettled` + `.catch()` on the 11 inline
   writes
10. CSP + `nosniff`; rate limiting on `/api/ask` and `/api/login`;
    `--workers 4 --timeout 120`; upper-bound `flask` / `anthropic`
11. Widen `WRITE_RE` + full-text scan; point `test_architecture` at
    `ontology._verbs()`
12. Kill the `txn_to_json` N+1; batch `spending_summary` in `_monthly_series`;
    build one `_purchase_index`
13. `GET /api/vocabulary`; `agent_read_tools` imports `actions`; align the minus
    glyph
14. Delete the 7 tautologies; fix the 2 false docstrings; add
    `set(VERB_TOOL) == set(PARAM_SPECS)`
15. Extend the gate's snapshot with income + per-category; split the tripwire
    into mandatory / non-refund checks
16. `app.py` → blueprints; then `actions.py` / `derivations.py` → packages
    (guards updated in the same commit)

---

## Through-line

**The code is in better shape than the guards around it.** The verbs, SQL, scope
model and audit trail survived adversarial reading from three independent
directions. What did not survive is the meta-layer — the tripwire, the raw-write
regex, the render build guard, the balance gate's snapshot scope, and several
coherence tests. Each is trusted in the documentation at a strength it does not
have.

That gap is the most valuable thing this review found, because it is invisible
from inside a green suite.
