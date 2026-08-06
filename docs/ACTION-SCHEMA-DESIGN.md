# ACTION-SCHEMA-DESIGN — declarative action parameters

Ontology convergence #2. CORE-DESIGN frames the whole architecture as "Palantir's
Ontology, scaled to a Pi" — importing the **grammar** (typed nouns, governed
verbs, policy at one chokepoint, aggregates on read) and refusing the **engine**.
This closes the one place the "governed verbs" grammar still leaks: an Action's
**parameter contract** is not single-sourced the way its write-path is.

Checked against CORE-DESIGN; it governs. This is a *consolidation* of the action
grammar, not a new capability.

## The problem, concretely

One write action's parameters are hand-maintained in three-to-four places. Take
`classify_inflow`'s `income_type` vocabulary `{paycheck, reimbursement, refund,
transfer, gift, other}`:

- `actions.py` — `INCOME_TYPES` frozenset (the real source, incl. `unclassified`);
- `agent_write_tools.py` — `REAL_INCOME_TYPES = [...]`, redeclared as a JSON-schema
  `enum` in the Ask tool's `input_schema`;
- `ledger_mcp.py` — the vocabulary in a `Field(description="One of: paycheck, …")`
  **prose** string, not even enforced as an enum;
- the tool description blurbs — the types listed again in English.

Same for item `kind`/`status`: `ITEM_KINDS`/`ITEM_STATUSES` frozensets vs. the
hand-written `["staple","oneoff"]` / `["stocked","low","out"]` enums in the Ask
tool schema vs. the MCP type hints. **Failure mode:** add a fourth item status in
a migration + verb (exactly the kind of increment this project does), and the
agent tools keep offering the stale three, silently — no test binds them.

## The boundary: structural vs. semantic (what must NOT move)

- **Unifiable (structural):** parameter names, types, required-ness, and the enum
  vocabularies that are *already verb constants*.
- **Stays in the verb (semantic):** category-exists, rule-conflict, member-active,
  `settle_up` deriving its amount from the balance, cross-field checks. JSON-schema
  can't express these and shouldn't. Palantir layers Action *parameter types*
  beneath Action *validation logic*; so does this. The declarative layer is the
  parameter **shape**, not the business rules.

## Decisions (Alta, Aug 5, 2026)

- **Full spec** — a declarative parameter registry per write verb that GENERATES
  the Ask + MCP tool schemas (steps 1–3); verb-side validation + a generated
  CORE-DESIGN table are optional later work (steps 4–5).
- **Write verbs only** — the strong case is verb-parameter-contract ↔ agent
  write-tool-schema. Read tools wrap *endpoints* (month/anchor/months_back query
  params), not verbs, so folding them in is a separate, weaker endpoint-signature
  unification — out of scope here.

## The design

One declarative parameter spec per write verb, in `actions.py` (where invariant
2's source of truth already lives), whose enum fields **reference the existing
constants** (`INCOME_TYPES`, `ITEM_KINDS`, `ITEM_STATUSES`) — no new copies. It is
the single source that:

1. GENERATES the Ask write-tool `input_schema` (`agent_write_tools`) and the MCP
   write-tool schema (`ledger_mcp`) — both stop being hand-written;
2. *(optional, later)* does first-pass STRUCTURAL validation in the verb
   (type/required/enum) before the verb's semantic checks.

This extends a pattern already proven on the read side: `agent_read_tools.
DESCRIPTIONS` is the single source both doors consume, with a drift test asserting
`ledger_mcp`'s live descriptions equal `DESCRIPTIONS`. #2 carries that same
shared-source discipline from tool *descriptions* to tool *parameters*.

Shape (illustrative, not final):

```
# actions.py — next to the verbs
PARAM_SPECS = {
  "classify_inflow": [
    Param("transaction_id", int, required=True, desc="…"),
    Param("income_type", str, enum=REAL_INCOME_TYPES, required=True, desc="…"),
  ],
  "add_item": [
    Param("name", str, required=True, desc="…"),
    Param("kind", str, enum=sorted(ITEM_KINDS), default="staple", desc="…"),
    Param("status", str, enum=sorted(ITEM_STATUSES), desc="…"),
  ],
  ...
}
# → param_schema("add_item") yields the JSON-schema object both tool surfaces use.
```

(`REAL_INCOME_TYPES` = `INCOME_TYPES − {'unclassified'}`, derived once from the
frozenset, not re-typed.)

## Fits "grammar, not engine"

A consolidation, not new capability. Touches only the parameter *declaration*
layer — verbs' semantic validation, the routes, and every money path are
untouched. **No schema, no migration, no balance gate** (a tooling/read-surface
refactor, like the agent-layer increments). Tool count and behavior stay
identical; only where their schemas come from changes. It makes the action
registry machine-real instead of prose — the exact ontology property CORE-DESIGN
says it imports.

## Build order

1. `PARAM_SPECS` (or an `@action(params=…)` decorator) in `actions.py`, enums
   referencing the vocab constants — single source, ZERO behavior change; a test
   proves `param_schema(verb)` is byte-equal to today's hand-written schema for
   every write verb.
2. `agent_write_tools` builds each tool's `input_schema` from the spec (delete the
   hand-written blocks); equality test.
3. `ledger_mcp` write tools consume the spec; extend the existing drift test from
   descriptions to parameters.
4. *(optional)* first-pass structural validation in verbs from the spec —
   **must preserve the exact error-message strings the v1.0 parity tests pin**, so
   staged and behind its own gate-green proof.
5. *(optional)* generate / coherence-check the CORE-DESIGN action-registry table
   against the spec, so the prose table can't drift from code either.

Steps 1–3 kill the drift with no behavior risk; 4–5 are the deeper convergence.

## Deliberately out of scope

- **Read tools** — they wrap endpoints, not verbs; a separate unification.
- **Semantic validation in schema** — stays in the verb (see the boundary above).
- **A general permission/ACL layer, object-change event pipelines, lineage** —
  Ontology *engine* features CORE-DESIGN refuses; not part of the grammar.
