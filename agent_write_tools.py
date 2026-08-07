"""agent_write_tools — the write-tool surface for Charlee's in-app Ask loop.

Kept SEPARATE from agent_read_tools (which is read-only by construction): the
in-app assistant may TAG inflows, and only that. Each tool bottoms out in the
same Flask write ROUTE the SPA uses (one write path, CORE-DESIGN invariant 2),
executed in-process under the caller's own session — so the write is attributed
to the person (`ui:<name>`), validated by the verb, logged, and reversible.

Tools exposed: classify_inflow (tag an inflow) and the household-pantry set —
add_item, set_item_status, archive_item (remove), set_item_match — the pantry
is groceries/supplies, never money, so it needs no two-phase choreography and
gets broad conversational control (INVENTORY-DESIGN: direct writes like
classify; even 'remove' is a reversible soft-delete). Rules (create_income_rule
/ apply_rules, two-phase) are a later increment; settle-up, transaction edit or
delete, and money movement deliberately have NO tool here — the money line held
on purpose (AGENT-DESIGN invariant 3). The pantry READ ('what do we need?', and
finding an item's id) is the shared ledger_inventory read tool, not here.
"""
from typing import Callable

import actions   # the single source for each write verb's parameter schema
                 # (ACTION-SCHEMA-DESIGN): input_schema = actions.param_schema(verb)

WRITE_TOOLS = [
    {
        "name": "ledger_classify_inflow",
        "description":
            "Tag ONE piece of money that came IN with what kind it is, AFTER "
            "the person has told you in their own words what it was (e.g. they "
            "say 'that $1,041 deposit was my paycheck'). Only works on money-in "
            "rows — find the id with ledger_unclassified_inflows or "
            "ledger_search_transactions. It is reversible (call again with a "
            "different type to fix a mistake) and logged. What each type does to "
            "the numbers: paycheck counts as real income; refund subtracts from "
            "that category's spending; transfer, reimbursement, and gift/other "
            "are kept out of income. NEVER guess the type — if you're not sure, "
            "ask. You cannot move money, record a settle-up, or edit or delete "
            "anything here; for those, tell them to use the app.",
        "input_schema": actions.param_schema("classify_inflow"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/transactions/{a['transaction_id']}/classify",
            {"income_type": a.get("income_type")}),
    },
    {
        "name": "ledger_add_item",
        "description":
            "Add something to the household pantry. Two kinds: a STAPLE is "
            "something to keep tracked ongoing (coffee, dish soap); a ONE-OFF is "
            "a single thing to buy this once (birthday candles) — it lands on the "
            "shopping list and disappears once bought. Use kind='oneoff' when "
            "they just need to buy something once, kind='staple' for a thing they "
            "want to keep an eye on. If they said it's already low or out, pass "
            "that as status. Logged; there's no undo here (removing a tracked "
            "item happens in the app). This is groceries/supplies — it never "
            "touches money.",
        "input_schema": actions.param_schema("add_item"),
        "execute": lambda caller, a: caller("POST", "/api/inventory", {
            k: a[k] for k in ("name", "kind", "status", "note") if a.get(k)}),
    },
    {
        "name": "ledger_set_item_status",
        "description":
            "Mark a pantry item stocked, low, or out — the everyday update "
            "('we're out of milk', 'down to the last roll', 'restocked the "
            "coffee'). Find the item's id first with ledger_inventory. Marking a "
            "ONE-OFF need 'stocked' means it was bought, so it drops off the "
            "shopping list. Reversible (set it again) and logged.",
        "input_schema": actions.param_schema("set_item_status"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/inventory/{a['item_id']}", {"status": a.get("status")}),
    },
    {
        "name": "ledger_archive_item",
        "description":
            "Remove an item from the pantry — stop tracking a staple they no "
            "longer want to watch, or drop a one-off need. Find the item's id "
            "first with ledger_inventory. It's a reversible soft-delete (the item "
            "just stops showing up) and logged. Pantry only — never money.",
        "input_schema": actions.param_schema("archive_item"),
        "execute": lambda caller, a: caller(
            "DELETE", f"/api/inventory/{a['item_id']}", None),
    },
    {
        "name": "ledger_set_item_match",
        "description":
            "Teach Ledger how a staple shows up on the bank feed, so it can spot "
            "when they've restocked it. Pass a purchase phrase (e.g. 'chewy' for "
            "dog food, 'trader joe' for a store), or an empty string to clear it. "
            "Find the item's id with ledger_inventory first. Logged and "
            "reversible; pantry only.",
        "input_schema": actions.param_schema("set_item_match"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/inventory/{a['item_id']}",
            {"restock_match": a.get("restock_match", "")}),
    },
]

_BY_NAME = {t["name"]: t for t in WRITE_TOOLS}
WRITE_TOOL_NAMES = frozenset(_BY_NAME)


def anthropic_write_tools() -> list:
    """The write tools in Anthropic Messages API format. No cache_control here —
    the loop appends these after the read tools and marks the LAST tool of the
    combined block, so the whole static tool list is one cache breakpoint."""
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t["input_schema"]} for t in WRITE_TOOLS]


def call_write_tool(caller: Callable[[str, str, dict], dict],
                    name: str, args: dict) -> dict:
    """Execute one write tool via `caller(method, path, body) -> dict`. Raises
    KeyError for an unknown tool name (the model hallucinated one); the caller
    raises on a non-2xx write, which the loop catches and hands back to the
    model as a recoverable tool error."""
    spec = _BY_NAME.get(name)
    if spec is None:
        raise KeyError(f"unknown write tool: {name}")
    return spec["execute"](caller, args or {})
