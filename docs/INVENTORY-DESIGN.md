# Design: Household Inventory ("the pantry")

Ledger's first step beyond money: a shared, low-upkeep way to track the
household staples the two of you don't want to run out of — and a shopping
list — so you never buy the same thing twice or run too low.
Status: **design only, not built. MVP scope.**

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
