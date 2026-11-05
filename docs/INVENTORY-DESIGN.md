# Design: Household Inventory ("the pantry")

Ledger's first step beyond money: a shared, low-upkeep way to track the
household staples the two of you don't want to run out of — and a shopping
list — so you never buy the same thing twice or run too low.
Status: **fully built.** The MVP + step 5 (migrations #008–#011) are live, and
the **Pantry v2 amendment at the bottom (Aug 20, 2026) was built end to end on
Aug 21** — increments 1–7 (batch restock, #014 store/need_by/snooze, the
status-derived cadence, the priced list + price drift, the trip composition,
the hygiene layer with the Sunday pulse, and the `ordered` status via #015).
The original design below stands as written.

## The core claim

Every standalone inventory app dies from one thing: **upkeep**. Nobody
maintains a list of what's in their cupboards. Ledger is uniquely placed to
beat that, because it already has the two things that make an inventory
*self-maintaining*:

1. **The bank feed** already sees what you *buy* (SimpleFIN → transactions with
   merchant/description). Purchases are the ground truth of what enters the
   apartment, so inventory can eventually populate itself instead of by hand.
2. **The Ask assistant, with write access,** is already a conversational way to
   change data. "We're out of dish soap," "add coffee," "do we still have paper
   towels?" is the lowest-friction capture there is — and the pipe (Ask tab →
   `classify_inflow`-style verbs) is already built (AGENT-DESIGN).

That combination — *purchases in, conversation to correct* — is the whole
reason to build this **inside** Ledger rather than download a separate app. No
standalone inventory app has your spending data or a chat that can write.

**The discipline that keeps it alive: track the ~20–30 things you'd hate to run
out of, not everything.** Curated staples, not an exhaustive apartment
inventory. Exhaustive is the trap; curated is maintainable.

## Settled decisions (recommended; open to Alta/Charlee's redirect)

- **Household-scoped, member-count-agnostic.** Items belong to the household,
  not a person — same posture as bills and goals (CORE-DESIGN invariant 3).
  No per-person ownership in v1.
- **One unified model, list is derived.** A single `items` table; each item
  carries a status. The "shopping list" is a *derivation* (items that need
  buying), never a second stored table (invariant 6). A one-off need (party
  candles) is just an item flagged `oneoff` that archives itself once bought.
- **Three states, no quantities.** `stocked | low | out`. Quantities are
  upkeep hell ("how many rolls left?") — deliberately excluded from v1. Status
  changes with one tap or one sentence.
- **Chat is a first-class input from day one.** The killer interaction is
  telling the assistant, so `set_item_status`/`add_item` are exposed to the Ask
  loop as direct writes (the exact pattern `classify_inflow` already uses —
  logged, reversible, human-confirmed in conversation).
- **Suggest, don't assert** (inherited from INCOME-DESIGN). When purchase-feed
  inference lands later, "you bought coffee 2 weeks ago" is a *hint* a human
  confirms — buying coffee ≠ having coffee now (you may have used it).
- **Deferred to later increments, on purpose:** purchase-feed auto-population,
  restock *prediction* from purchase cadence, barcode/camera capture,
  quantities, where-is/location, and the money tie-in ("$40/mo on coffee").
  Each is a clean add onto the MVP, not a prerequisite.

## Nouns

Conventions unchanged: integer where counted, ISO-8601 text timestamps; where a
deployed spelling later differs, deployed wins.

```sql
-- The household's tracked things. Curated staples + transient one-offs.
CREATE TABLE items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    category    TEXT,                         -- reuse the txn category vocab; nullable
    kind        TEXT NOT NULL DEFAULT 'staple' -- 'staple' | 'oneoff'
                CHECK (kind IN ('staple', 'oneoff')),
    status      TEXT NOT NULL DEFAULT 'stocked'-- 'stocked' | 'low' | 'out'
                CHECK (status IN ('stocked', 'low', 'out')),
    note        TEXT,                          -- "the big blue jug", brand, store
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1     -- soft delete; history stays attributable
);
```

Why this shape: one table, no joins for the common reads. A one-off is an item
with `kind='oneoff'` — it lives on the shopping list (its `status` starts
`out` = "need") and is archived (`active=0`) once bought, so the list stays
clean. A staple stays forever; its `status` cycles as you use and restock it.
No quantities, no per-person columns — the two hardest-to-maintain things,
refused.

## Verbs

The action registry grows (CORE-DESIGN invariant 1): each verb in `actions.py`,
same contract (validate → edit → side effects → audit). Actor vocabulary
shared: `ui:<member>` | `sync` | `mcp:<label>`.

| Verb | Callers | Submission criteria | Notes |
|---|---|---|---|
| `add_item` | UI, MCP, Ask | name non-empty; `kind`/`status`/`category` valid | Creates a staple or a one-off need; audit carries the created shape |
| `set_item_status` | UI, MCP, Ask | item exists & active; status ∈ vocab | The core interaction (mark low/out/stocked). A one-off set `stocked` archives itself (bought → gone from the list) |
| `rename_item` / `set_item_note` | UI | item exists | Small edits; audit records before/after |
| `archive_item` | UI, MCP | item exists | Soft delete (`active=0`), matching `delete_goal`/`delete_bill`'s bounded posture; history untouched |

`set_item_status` is the routine action — one tap, one sentence — so it's a
**direct** write in the agent tiers (logged, reversible), exactly like
`classify_inflow`. Nothing here moves money or touches the balance, so none of
it needs the two-phase choreography.

## Derivations

All read-time, named, consumed by every surface (invariant 6):

- `shopping_list(db)` — everything that needs buying: staples with
  `status IN ('low','out')` **plus** active one-offs. The "what do we need?"
  read the SPA and the assistant both call.
- `low_stock(db)` — staples at `low`/`out` (the nudge / badge count).
- *(deferred)* `restock_forecast(db, item)` — projected run-out date from
  purchase cadence, once the bank feed feeds inventory. Rides the same
  `_monthly_series` trend engine the analytics use.

## Policies

- **Identity/visibility:** household-scoped; `visible_to(row, member)` returns
  `True` (the existing stub), so the future per-person answer lives in one
  place if ever wanted.
- **Scopes:** the Ask/MCP write tools reach `add_item`/`set_item_status` under
  the existing `read,write` gating — no new auth mechanism.

## Build order (increments, one per session, each deployable + gated)

0. **This design doc**, checked against CORE-DESIGN. ← you are here
1. **Migration #008 — `items`** (schema only; enumerated-diff gate: one empty
   table + `schema_version` bump, like #006/#007).
2. **Verbs + derivations + read endpoints** — `add_item`, `set_item_status`,
   `archive_item`; `shopping_list` / `low_stock`; `GET /api/inventory` and the
   thin write routes. Zero-diff balance gate (no money path touched); tests.
3. **The SPA surface** — an "Inventory" view (Garden-styled: staple rows with a
   tap-to-cycle stocked/low/out chip, a shopping-list section, an add field).
   Reached from a **Home shortcut pill** (like Bills/Analytics) — the 5-slot
   nav stays as designed; a dedicated nav slot is a later IA call.
4. **Chat input** — expose `add_item`/`set_item_status` to the Ask loop (extend
   the write-tool surface): "we're out of X", "add Y", "what do we need?".
5. **Later:** purchase-feed auto-population + restock prediction; then
   quantities / barcode / money tie-in, each as its own increment.

Steps 1–4 are the MVP: a shared list + staples tracker you maintain by tapping
*or* talking. Everything that makes it feel magic (self-population, prediction)
is step 5+, added once the basics stick.

## Deliberately out of scope / refused

- **Exhaustive inventory** — track the staples you'd hate to run out of, full
  stop. A complete apartment catalogue is the thing nobody maintains.
- **Quantities in v1** — have/low/out only; counting units is upkeep death.
- **Per-person ownership** — household-scoped like bills/goals.
- **Auto-population from purchases in v1** — powerful, but a *hint* layer added
  after the manual/chat basics exist (and always human-confirmed).
- **A second app / a separate chores or calendar feature** — those are their
  own designs if ever wanted; this doc is only the pantry.

## Amendment to the other design docs

- **CORE-DESIGN.md**: the inventory nouns/verbs join the registry; this is the
  **first non-finance domain** — Ledger's identity widens from "household
  finance" to "the shared household," deliberately (Alta's call, Aug 3). The
  grammar is built to extend this cheaply; the money invariants (2, 4, 6) are
  untouched because inventory never touches money.
- **AGENT-DESIGN.md**: the Ask/MCP write tools gain `add_item`/`set_item_status`
  as direct writes (no money movement, so no two-phase). Otherwise stands.

---

# Amendment (Aug 20, 2026): Pantry v2 — after the MVP stuck

The MVP and step 5 are deployed and in daily use; the discipline held (curated
staples, no quantities, suggest-don't-assert). This amendment sequences the
next wave.

> **Correction (same day, after the rework↔main sync):** the main lineage had
> already built three of this amendment's ideas before the sync surfaced them:
> `staple_spend` (≈ the planned `item_spend` — per-staple monthly cost),
> `last_shopping_trip` (the post-shopping review nudge — a lean INTO the
> merchant-not-product limit that partially covers "trip closure"), and
> `stale_shopping_items` (list-rot on low/out staples; the planned
> `stale_staples` — stocked-forever rot — remains distinct and unbuilt). So
> increment 4 below shrinks to `list_estimate` + the price-per-restock trend,
> and increment 5's trip-closure half starts from `last_shopping_trip` rather
> than from scratch. **Increment 1 (`restock_items`) shipped the day of this
> amendment.** The rest stands as written. Nothing here revisits a settled decision — every idea below is an
add that the original grammar was built to take cheaply.

## The parameter grammar, now codified

Steps #009/#011 established a pattern worth naming as a rule for every future
pantry parameter:

> **Explicit nullable override; NULL means infer or fall back.**
> `restock_match` NULL → match on the name. `restock_interval_days` NULL →
> infer the cadence. Every new per-item parameter follows this shape: a
> structured, migration-owned nullable column whose NULL state is the smart
> default, so a fresh item needs zero configuration and every parameter is
> opt-in per item.

Refused alternatives, permanently: a generic `item_attrs` JSON blob (bypasses
verb validation and the schema grammar — invariant 1/2 by the back door), and
any parameter whose non-NULL state demands recurring upkeep (the quantities
lesson generalized: a parameter you set once is fine; a parameter you must
*maintain* is the trap).

## The untapped signal: status transitions

Every `set_item_status` is already audited with a timestamp. So stocked→out
durations are sitting in `audit_log` — **human-confirmed consumption cadence,
immune to the merchant-not-product limit** that hobbles purchase matching. A
staple bought inside supermarket runs (invisible to `_matching_purchases`)
still tells us its rhythm every time someone taps its chip. This is the
highest-leverage unbuilt thing in the pantry, and it needs no migration.

`restock_forecast`'s source ladder widens from two rungs to three:

  manual (`restock_interval_days`) → **status-derived (median stocked→out gap
  from audit history)** → inferred (median purchase gap, ≥3 matches)

Since forecasts recompute on every read (invariant 4), predictions tighten
automatically as cycles accumulate — no stored model, no training step.

## New nouns (one migration, #014 — #012/#013 went to the transfer increments)

```sql
ALTER TABLE items ADD COLUMN store TEXT;      -- where it's bought; NULL = ungrouped
ALTER TABLE items ADD COLUMN need_by TEXT;    -- ISO date, mostly one-offs; NULL = whenever
ALTER TABLE items ADD COLUMN snoozed_until TEXT; -- pause nudges; NULL = live
```

All three obey the grammar (NULL = today's exact behavior; gate is
schema-version-bump-only, mirroring #009/#011). `store` unlocks the classic
killer read — the shopping list grouped by store, "I'm at Costco, what do we
need *here*?" — set once per item, zero upkeep. `need_by` lets the Ask loop
honor "we need candles before Saturday" and sorts the list by urgency.
`snoozed_until` covers travel ("stop nudging about milk") without widening the
status vocab.

**Debate before building, with Charlee:** a fourth status `ordered` (bought
online, not arrived, don't re-buy). It prevents real double-buys but adds a
state a human can forget to clear; if adopted, the feed should offer to clear
it when the matching purchase lands. Parked until the household wants it —
`snoozed_until` may cover enough of the need.
*Decided and built Aug 21, 2026 (increment 7, migration #015 — a table
rebuild, since SQLite can't widen a CHECK):* `ordered` leaves the shopping
list without counting as stocked; an "On the way" drawer offers Arrived (→
stocked; a one-off then archives as bought) / Didn't come (→ out). The
anti-forget mechanism is NOT the feed (an online order's charge lands at
order time, proving nothing about arrival) but a view-layer "still
waiting?" after 7 days and a line in the weekly pulse. Re-ordering from
`stocked` counts as a cycle departure for the status cadence (it's the
household saying "running down").

## New verbs

| Verb | Callers | Notes |
|---|---|---|
| `restock_items` | UI, Ask | **The trip verb**: one action marks a bought set stocked (each item validated + audited individually inside the verb). Kills the worst daily friction — five taps after one grocery run |
| `set_item_store` / `set_item_need_by` / `set_item_snooze` | UI, Ask | Small setters, `set_item_interval`'s exact pattern; NULL-in clears |

No new auth, no two-phase — nothing touches money.

## New derivations (all read-time, all tripwire-checked)

- `item_history(db, item_id)` — one item's status timeline from `audit_log`;
  makes the forecast explainable ("predicted from your last 4 cycles").
- `item_spend(db)` — the money tie-in the original design deferred: per-staple
  monthly cost via `_matching_purchases` + `_monthly_series`. "Coffee: ~$42/mo."
- `list_estimate(db)` — the shopping list priced: each item's typical restock
  cost, summed. "This trip ≈ $85." (Ask: "what will the list cost?")
- Price-per-restock trend inside `item_spend` — surfaces "dish soap is up 30%
  since March" through the existing anomaly posture.
- `trip_plan(db, horizon_days=7)` — shopping list **plus** staples the forecast
  predicts go low within the horizon: "going anyway? also grab coffee, due
  Tuesday." Turns the forecast from a badge into a decision aid.
- Trip closure (extends `restock_suggestions`): when one grocery outflow lands,
  offer the *set* — "Costco run yesterday: restock these 4 low items?" — one
  confirm feeding `restock_items`. Suggest-don't-assert, grouped by
  transaction instead of by item.
- `stale_staples(db)` — the curation guard, mirror of `unmatched_staples`:
  staples with no status change and no matched purchase in ~6 months, offered
  for archive. This is what keeps the set at ~20–30 forever instead of
  silently growing into the exhaustive-inventory trap.

**Tripwire honesty (CLAUDE.md, CODE-REVIEW #8):** `item_spend`,
`list_estimate`, `trip_plan`, and trip closure all read transactions, so each
must be confirmed *actually reachable* by the tripwire fixture's contaminating
inflows (or get a hand-written `*_ignores_inflows` test) — "tripwire-covered"
is claimed per-derivation, verified by watching it fail once. They also
inherit the v12/v13 transfer posture: `is_transfer` rows are excluded
alongside inflows and settlements, per the merchant/pantry
transfer-consistency rule — a credit-card payment described "COSTCO"
must never price the shopping list.
`item_history`/`stale_staples` read no transaction amounts but must still
never count an inflow as a "matched purchase" (they bottom out in
`_matching_purchases`, which already filters).

## Surfacing (no new mechanism, existing rails)

- **Garden**: one ambient line when the list is non-empty ("3 things on the
  list"). The greeting infrastructure already exists.
- **Weekly pantry pulse** — *corrected at build (inc 6):* NOT a cloud
  routine (they can't reach the Pi, and the pantry lives only there) but a
  Pi-side timer like the guardian — `deploy/pantry_pulse.py` reads the named
  `pantry_pulse` derivation through the app's own API with a read token and
  files a GitHub issue over the existing alert bridge. Predicted-low this
  week, stale candidates, list rot, one new-staple suggestion.
  *Retired Nov 5, 2026:* the issue job is gone (nobody read the issues);
  restocks due and orders waiting are Ask check-ins, and stale staples, list
  rot and suggestions stay on the Pantry tab. The `pantry_pulse` derivation
  and route remain for the Ask/MCP read tool.
- **Ask**: the new derivations and verbs join the read/write tool surface
  under the existing scopes — "what will the list cost?", "we got everything
  at Costco", "snooze the milk until the 30th".

## Build order (per-increment, each deployable + gated)

1. **`restock_items`** (verb only) — biggest daily-friction win, zero schema.
2. **Migration #014** (`store`, `need_by`, `snoozed_until`) + setters +
   `shopping_list` grouped/sorted/filtered by them. Gate: schema bump only.
3. **Status-derived cadence** — `item_history` + the third `restock_forecast`
   rung. No migration; the forecast gets smarter for free thereafter.
4. **Money tie-in** — `item_spend` + `list_estimate` (+ price trend).
5. **Trip composition** — trip closure in `restock_suggestions` + `trip_plan`.
6. **Hygiene layer** — `stale_staples` + the weekly pulse + the Garden line.
7. *(if the household wants it)* the `ordered` status, post-debate.

Steps 1–3 are the daily-feel wave; 4–5 make it magic; 6 keeps it alive for
years. Every step is one verb, one migration, or one derivation — none
touches a money write path, so all gate zero-diff except #014's enumerated
schema bump.

## Still refused (v2 edition)

Everything the original list refused, plus, examined and declined this round:
recipes/meal planning (scope trap — a different product), barcode/camera
capture (heavier than one sentence to Ask, which already exists), quantities
(still upkeep death — `need_by` and notes cover the real cases), per-person
ownership, a priority field (derivable from status + `need_by`), and the JSON
attrs blob (see grammar).

---

# Amendment (Nov 3, 2026): receipt scan

Alta asked for it directly: "taking a picture of the receipt and then
translating that and then having that information associate the product with
the store that it was bought from … This way, we won't have to manually enter
items." The v2 list above refused **barcode/camera capture** as heavier than one
sentence to Ask. That refusal stands for barcodes (a scan per item is upkeep).
A receipt is different: **one photo per trip** covers every line, and it is the
only input that says *which store* each thing came from, which is what the #014
store-grouped list needs and what nobody fills in by hand.

**Flow.** Pantry → "📷 Scan a receipt" → 1–3 photos (a long receipt in parts) →
`POST /api/receipts/read` reads them into a draft and **saves nothing** → the
review sheet shows every printed line with a pre-picked choice (restock an
item / add as a new staple / skip) that the person can change → Save →
`POST /api/receipts` → the `record_receipt` verb.

**Reading.** One Anthropic Messages call (the Ask tab's `ANTHROPIC_API_KEY`;
model `RECEIPT_MODEL`, default `claude-opus-5-5`) with a strict tool schema;
the tool input is the draft. Chosen over on-Pi OCR (Tesseract) because receipt
abbreviations ("GV 2% MLK GAL") need a reader that knows groceries, and the Pi's
CPU stays free. The model's output is untrusted input: every field is bounded
and re-parsed (`receipt_reader.normalize`, Decimal → integer cents). Session-only
(a bearer token can't spend money), 20 reads/hour/person, photos ≤ 5 MB each,
type checked from the bytes. **Photos are never stored** — held in memory for
the one call. The phone shrinks them to ~2000px JPEG before upload.

**Memory without a new table.** `receipt_lines` keeps each printed line with the
item it became (or NULL for skipped). The next review sheet looks up the newest
line with the same printed text — at the same store first, then anywhere — so
"GV 2% MLK GAL" is Milk forever after one confirmation. Computed on read
(`receipt_line_suggestions`); no alias table, no stored match key. Then the
reader's own guess at an existing item, then a name match, then "add as new"
for products and "skip" for fees, tax and discounts.

**Store.** The household's spelling wins (`receipt_canonical_store`: "SHOPRITE
#312" → the "ShopRite" the list already groups by). Save fills the store only on
items that have none; an item's existing store is never overwritten.

**Money: none.** `record_receipt` writes `receipts`, `receipt_lines`, `items`
and `audit_log` only. The optional bank-charge link (`receipts.txn_id`, offered
from `receipt_charge_candidates`: outflows within 3 days on the exact total or a
store word, never a transfer/settlement/Ledger-logged bill, one receipt per
charge) is a pointer for later questions ("what did that $84 ShopRite trip
buy?"); it changes no amount, split or balance. Gate expectation: schema 17→18
plus two empty tables (`notes/018-gate-expectation.seed.json`).

**Still refused here:** quantities (the receipt's count is recorded on the line,
never as pantry stock), per-item prices as a budget, and auto-saving without the
review sheet.
