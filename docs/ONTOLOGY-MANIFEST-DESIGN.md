# ONTOLOGY-MANIFEST-DESIGN — the ontology as one introspectable artifact

**Status: proposed, awaiting Alta's review. No code touched yet.**

Ontology convergence #3, following ACTION-SCHEMA-DESIGN (#2). CORE-DESIGN frames
the architecture as "Palantir's Ontology, scaled to a Pi" — importing the
**grammar** (typed nouns, governed verbs, one write chokepoint, aggregates on
read) and refusing the **engine**. The comparison exercise (Aug 6) surfaced the
one Foundry property the grammar claims but doesn't deliver: **introspection**.
Foundry's ontology is a single queryable artifact; Ledger's is *true but
scattered* — you can only see the whole by reading four places:

| Fact | Where it lives today |
|---|---|
| What the object types are | `GOVERNED_TABLES` — in `tests/test_architecture.py` (!) |
| What the Actions are | `actions.py` defs + CORE-DESIGN's prose registry table |
| What each Action's parameters are | `PARAM_SPECS` (3 agent-exposed verbs only) |
| What the Functions are | `derivations.py` defs; exemptions in the tripwire test |
| What vocabularies exist | `*_ORDER` tuples in `actions.py` |
| Which migration created what | filenames in `migrations/` |

No single map a person — or a tool, or a test — can read. This design makes the
manifest real: **`ontology.py`, a generated, coherence-tested, read-only module**
plus **`GET /api/ontology`**, assembled from the sources above. Nothing is
hand-maintained that can be derived; everything hand-maintained is drift-tested.

## Fits "grammar, not engine"

This is *pure introspection over what already exists* — the strongest possible
fit with the project's "single source, drift-tested" ethos (the same pattern as
`agent_read_tools.DESCRIPTIONS` and `PARAM_SPECS`, applied to the ontology
itself). Explicitly NOT in scope, per ACTION-SCHEMA-DESIGN's out-of-scope list
(engine features CORE-DESIGN refuses): permission/ACL layers, object-change
event pipelines, lineage graphs. The manifest *describes* the ontology; it
never *mediates* anything. No behavior changes anywhere.

## What the manifest contains (the shape)

`ontology.py` exposes one function, `manifest()`, returning a plain dict
(JSON-serializable, no live db needed — the ontology is a property of the CODE,
not of any database's contents):

```python
{
  "schema_version": 10,              # from migrate.py's REQUIRED_SCHEMA_VERSION
  "objects": [                       # one per governed table
    {
      "name": "transactions",
      "created_by_migration": "001*",   # earliest migration whose DDL creates it
      "written_by": ["record_transaction", "edit_transaction", ...],  # derived
      "governed": True,
    }, ...
  ],
  "actions": [                       # one per public verb in actions.py
    {
      "name": "classify_inflow",
      "writes": ["transactions", "income_rules", "audit_log"],  # derived by scan
      "params": {...} | None,        # param_schema(verb) where PARAM_SPECS has it
      "two_phase": False,            # True for the PROPOSABLE_ACTIONS pair's targets
      "registered": True,            # present in CORE-DESIGN's registry table
    }, ...
  ],
  "functions": [                     # one per public derivation
    {
      "name": "spending_summary",
      "reads_inflows": True,         # from the tripwire's EXEMPT set
    }, ...
  ],
  "vocabularies": {                  # the ordered enum tuples, by name
    "income_types": [...], "item_kinds": [...], "item_statuses": [...],
  },
  "doors": ["api", "mcp", "ask"],    # static — the three read surfaces
}
```

\* v1.0-era tables (`transactions`, `goals`, `bills`, …) predate the migration
runner; they get `"created_by_migration": "v1.0-baseline"`. Only 002+ objects
map to a numbered migration.

### How each field is derived (nothing invented)

- **objects** — from `GOVERNED_TABLES`, which MOVES to `actions.py` (see the
  relocation below). Migration provenance by scanning `migrations/` filenames +
  DDL for `CREATE TABLE` per table; v1.0 tables fall back to the baseline label.
- **actions.written_by / writes** — mechanically derived: scan each public verb's
  source (the same `def name(db, actor` discovery and `INSERT/UPDATE/DELETE`
  regex `test_architecture` already uses) for governed-table writes. The scanner
  is shared logic with the invariant-1 test, not a third copy of the regex.
- **actions.params** — `param_schema(verb)` where `PARAM_SPECS` covers it; `None`
  otherwise (the honest state: only agent-exposed verbs have declared params).
- **actions.registered** — the same CORE-DESIGN table parse the coherence test
  uses. The manifest *reports* registration; the existing test still *enforces* it.
- **functions** — introspect `derivations.py` for public db-taking callables
  (exactly the tripwire's discovery rule); `reads_inflows` from the tripwire's
  EXEMPT set, which MOVES alongside (see below) or is imported from the test —
  decided in increment 1, leaning: keep EXEMPT in the tripwire test (it is the
  *test's* allowlist; the manifest imports it read-only).
- **vocabularies** — the existing `*_ORDER` tuples, referenced not copied.

## The one structural fix: GOVERNED_TABLES moves home

Today the authoritative list of governed objects lives in a **test file**.
That was fine when its only consumer was the test; with a second consumer
(the manifest) it becomes a wrong-way dependency (`ontology.py` importing from
`tests/`). Fix, in increment 1: the set moves to `actions.py` (where invariant
2's source of truth already lives — the natural home: "the tables the action
registry governs" belongs next to the action registry), and
`test_architecture.py` imports it from there. Behavior-identical; the test's
teeth are unchanged; the carve-out comment about `members` moves with it.

## The endpoint

`GET /api/ontology` — `login_required` (session or read-scope bearer, like every
read), returns `manifest()` verbatim. No db read at all (the manifest is
code-derived), so no money shapes, no `{cents, display}`, no period params.
Deliberately NOT on `/api/analytics/*` — it describes the system, not the data.

**MCP/Ask exposure is deferred** to its own later increment if wanted
(`ledger_ontology` would let Alta's Claude ask "what verbs exist?" — nice, not
needed to bank the manifest). Keeping inc scope one-web-endpoint matches the
read-tier build pattern (endpoint first, doors later).

## What this buys (why it's the keystone)

1. **The bird's-eye view becomes code-emitted.** The Aug-6 ontology atlas was
   hand-assembled from four sources; `GET /api/ontology` makes the same picture
   a curl away, always current.
2. **Coherence tests get a spine.** Registry↔code, params↔tools, exempt↔reads
   checks all currently re-derive their own views; they can converge on the
   manifest over time (not in this increment — no test rewrites here).
3. **Future convergences get cheap.** Typed link vocabularies (steal #2), value
   types (#3), and universal preview (#4) each need "one place that knows the
   shape" — the manifest is that place, retro-fitted.
4. **It closes ACTION-SCHEMA-DESIGN's optional step 5** (generate/coherence-check
   the CORE-DESIGN registry table) as a natural consequence: the manifest knows
   both the code verbs and the registry rows, so asserting their equality is one
   test — included here as the manifest's own coherence test.

## Testing (the drift-test discipline, applied to the manifest itself)

- `manifest()` objects == GOVERNED_TABLES exactly; every object's `written_by`
  verb exists; every action's `writes` ⊆ governed tables.
- Every public verb in `actions.py` appears in `actions`; every public
  derivation in `functions` (re-using the discovery rules, so a new verb or
  derivation is manifest-covered automatically — the tripwire property).
- `registered` is True for every action (this IS step 5's coherence check).
- `params` for the three PARAM_SPECS verbs byte-equal `param_schema(verb)`.
- Endpoint test: 200 under session AND read-bearer; 401 unauthenticated;
  response JSON round-trips.
- **Teeth verification** (house rule): temporarily add an unregistered verb /
  drop a governed table and watch the right test fail, before trusting either.

## What this deliberately is NOT

- **Not stored anywhere.** The manifest is computed on read from source — the
  "nothing derived is stored" law applied to metadata. No new table, no cache.
- **Not a behavior change.** No verb, route, derivation, or money path is
  touched beyond adding the read-only endpoint. Tool schemas keep coming from
  `PARAM_SPECS` exactly as today.
- **Not lineage.** `audit_log` stays a log; the manifest describes *types*,
  never row-level history (engine feature, refused).
- **Not a new maintenance surface.** Anything in the manifest that could drift
  from code is *derived* from code; the few declared facts (doors, baseline
  label) are trivial and drift-tested where possible.

## Increment plan (one merge each, per the loop)

1. **`ontology.py` + relocate `GOVERNED_TABLES`** — `manifest()` with the full
   shape, the verb/derivation scanners (shared with test_architecture's regex),
   coherence tests incl. the step-5 registry check, teeth verified. No route yet.
   No schema/migration; **zero-diff balance gate** (code-only, no db path).
2. **`GET /api/ontology`** — thin route + auth tests. Zero-diff gate.
3. *(optional, later)* `ledger_ontology` MCP read tool + Ask registry entry —
   rides the shared-DESCRIPTIONS pattern; separate increment if wanted.

Estimated size: inc 1 is the substance (~a session), inc 2 is small.

## Open questions for review

1. **GOVERNED_TABLES relocation to `actions.py`** — agreed? (The alternative,
   `ontology.py` owning it, makes the manifest a source rather than a mirror;
   rejected here because invariant 1 is an *actions* concept.)
2. **Registry-table coherence as a hard test** (manifest `registered` must be
   all-True) — this makes CORE-DESIGN's prose table load-bearing in a second
   test. Existing coverage already fails on unregistered verbs, so this only
   adds the reverse direction implicitly. Fine?
3. Scope check: happy to defer the MCP tool (inc 3) — or want it bundled?
