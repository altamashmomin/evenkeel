"""agent_write_tools — the write-tool surface for Charlee's in-app Ask loop.

Kept SEPARATE from agent_read_tools (which is read-only by construction): the
in-app assistant may TAG inflows, and only that. Each tool bottoms out in the
same Flask write ROUTE the SPA uses (one write path, CORE-DESIGN invariant 2),
executed in-process under the caller's own session — so the write is attributed
to the person (`ui:<name>`), validated by the verb, logged, and reversible.

Tools exposed: classify_inflow (tag an inflow); recategorize_transaction (B1 —
relabel ONE spending row's category, a constrained facade over edit_transaction
whose schema can't reach amount/splits/description, so it only ever moves a
label — proven not to move the balance or any month total, and confirm-first in
the prompt); and the household-pantry set — add_item, set_item_status,
restock_items (the after-shopping batch: mark a bought set stocked in one
action), archive_item (remove), set_item_match, set_item_store /
set_item_need_by / set_item_snooze (the #014 metadata: where it's bought, a
deadline, pause-until), and set_item_interval (a user-set restock cadence,
"remind me every N days"); and add_bill / add_goal (B4 — create a recurring bill
DEFINITION or a savings-goal TARGET, confirm-first: both move no money, touch no
transaction, and never change the who-owes-whom balance; each bottoms out in the
existing POST /api/bills / POST /api/goals route → create_bill / create_goal
verb). The pantry is groceries/supplies, never money, so it
needs no two-phase choreography and gets broad conversational control
(INVENTORY-DESIGN: direct writes like classify; even 'remove' is a reversible
soft-delete). Rules (create_income_rule / apply_rules, two-phase) stay a later
increment; settle-up, a general transaction edit (amount/split) or delete, and
money movement deliberately have NO tool here — the money line held on purpose
(AGENT-DESIGN invariant 3), recategorize being the one narrow, label-only
exception. The pantry READ ('what do we need?', and finding an item's id) is the
shared ledger_inventory read tool, not here.
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
        "name": "ledger_recategorize_transaction",
        "description":
            "Move ONE spending transaction into a different category — the "
            "everyday relabel ('that Target run was Household, not Groceries'). "
            "CONFIRM FIRST: find the exact row with ledger_search_transactions, "
            "tell the person which transaction and the from→to category, and "
            "only do it once they say yes — never guess which one. Categories "
            "are just labels, so any name works and a new one creates that "
            "category. This ONLY changes the label: the amount, the split, the "
            "who-owes-whom balance, and the month's total all stay exactly the "
            "same (it's proven). Reversible (recategorize back) and logged. It "
            "is for spending rows; to tag money that came IN use "
            "ledger_classify_inflow. You still cannot move money, settle up, or "
            "edit an amount / delete a row here — those happen in the app.",
        "input_schema": actions.param_schema("recategorize_transaction"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/transactions/{a['transaction_id']}/recategorize",
            {"category": a.get("category")}),
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
            "Mark a pantry item stocked, low, out, or ordered — the everyday "
            "update ('we're out of milk', 'down to the last roll', 'restocked "
            "the coffee', 'I ordered the dog food' → ordered = bought online, "
            "not arrived: off the list, not yet stocked; 'it arrived' → "
            "stocked). Find the item's id first with ledger_inventory. Marking a "
            "ONE-OFF need 'stocked' means it was bought, so it drops off the "
            "shopping list. Reversible (set it again) and logged.",
        "input_schema": actions.param_schema("set_item_status"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/inventory/{a['item_id']}", {"status": a.get("status")}),
    },
    {
        "name": "ledger_restock_items",
        "description":
            "Mark SEVERAL pantry items stocked at once — the after-shopping "
            "update ('we got everything', 'picked up the milk, eggs, and "
            "coffee'). Find each item's id first with ledger_inventory. "
            "All-or-nothing: if any id is wrong, nothing changes. A ONE-OFF "
            "in the set counts as bought, so it drops off the shopping list. "
            "For a single item, ledger_set_item_status is the same thing. "
            "Reversible per item (mark it again) and logged.",
        "input_schema": actions.param_schema("restock_items"),
        "execute": lambda caller, a: caller(
            "POST", "/api/inventory/restock", {"item_ids": a.get("item_ids")}),
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
    {
        "name": "ledger_set_item_store",
        "description":
            "Remember where an item is bought ('we get dog food at Costco') so "
            "the shopping list can group by store. Find the item's id with "
            "ledger_inventory first. Pass an empty string to clear it. Logged "
            "and reversible; pantry only — never money.",
        "input_schema": actions.param_schema("set_item_store"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/inventory/{a['item_id']}",
            {"store": a.get("store", "")}),
    },
    {
        "name": "ledger_set_item_need_by",
        "description":
            "Set a deadline on something to buy ('we need candles before "
            "Saturday') — the shopping list sorts needed-soonest first. Work "
            "the YYYY-MM-DD out from what they said. Find the item's id with "
            "ledger_inventory first (add it with ledger_add_item if it's new). "
            "Pass an empty string to clear the deadline. Logged and "
            "reversible; pantry only — never money.",
        "input_schema": actions.param_schema("set_item_need_by"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/inventory/{a['item_id']}",
            {"need_by": a.get("need_by", "")}),
    },
    {
        "name": "ledger_set_item_snooze",
        "description":
            "Pause an item's nudges until a date ('stop reminding us about "
            "milk until the 30th' — travel, guests, whatever). It keeps its "
            "status but stops nagging until then. Find the item's id with "
            "ledger_inventory first. Pass an empty string to wake it now. "
            "Logged and reversible; pantry only — never money.",
        "input_schema": actions.param_schema("set_item_snooze"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/inventory/{a['item_id']}",
            {"snoozed_until": a.get("until", "")}),
    },
    {
        "name": "ledger_set_item_interval",
        "description":
            "Set how often a STAPLE should be restocked — a reminder cadence "
            "they set by telling you, e.g. 'remind me to restock coffee every "
            "two weeks' → days=14, 'buy dog food monthly' → days=30. Find the "
            "staple's id with ledger_inventory first. After this, the pantry's "
            "restock predictions count from the last time it was marked stocked "
            "instead of guessing from bank purchases. Logged and reversible (set "
            "a different number to change it; clearing a cadence happens in the "
            "app). Staples only — pantry, never money.",
        "input_schema": actions.param_schema("set_item_interval"),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/inventory/{a['item_id']}",
            {"restock_interval_days": a.get("days")}),
    },
    {
        "name": "ledger_add_bill",
        "description":
            "Add a recurring bill definition ('add our $85 electric bill, due "
            "the 12th'). A bill is a monthly definition — it is NOT a payment "
            "and moves no money; marking one paid happens in the app. CONFIRM "
            "FIRST: read back the name, amount, and due day and add it only "
            "once they say yes — don't invent an amount or a day they didn't "
            "give. Amount is in dollars; due_day is 1–31; category is optional "
            "(defaults to 'Bills'). Logged; remove one in the app. This creates "
            "a definition — it never touches the who-owes-whom balance.",
        "input_schema": actions.param_schema("create_bill"),
        "execute": lambda caller, a: caller("POST", "/api/bills", {
            k: a[k] for k in ("name", "amount", "due_day", "category")
            if a.get(k) is not None}),
    },
    {
        "name": "ledger_add_goal",
        "description":
            "Add a savings goal ('start a $2,000 vacation fund'). A goal is a "
            "target to save toward — creating it moves no money; logging "
            "contributions happens in the app. CONFIRM FIRST: read back the "
            "name and target (and date, if any) and add it only once they say "
            "yes — don't invent a target they didn't give. Target is in "
            "dollars; target_date is optional (YYYY-MM-DD). Logged; remove one "
            "in the app. This creates a target — it never touches the balance.",
        "input_schema": actions.param_schema("create_goal"),
        "execute": lambda caller, a: caller("POST", "/api/goals", {
            k: a[k] for k in ("name", "target", "target_date")
            if a.get(k) is not None}),
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
