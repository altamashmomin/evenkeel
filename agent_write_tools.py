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
verb); set_budget (B3 — set/change a category's monthly spending limit, another
confirm-first DEFINITION that moves no money and never touches the balance,
POST /api/budgets → set_budget verb, an upsert keyed on category); and the rule
pair (B2) — propose_rule / confirm_action, the ONE surface
here that is two-phase BY THE SERVER, not just by the prompt: proposing parks a
frozen payload + dry-run preview in pending_actions and returns a single-use
token (nothing written), and confirming executes exactly the frozen payload —
so the model structurally cannot create a rule the person never saw, drift the
params between preview and execution, or double-execute (create_income_rule's
own docstring mandates agents reach it two-phase, never raw). The Ask facade is
narrow (match_desc + set_type only; set_type='transfer' creates a transfer
rule); amount-bounds/account/priority rules stay app/MCP territory. The pantry
is groceries/supplies, never money, so it needs no two-phase choreography and
gets broad conversational control (INVENTORY-DESIGN: direct writes like
classify; even 'remove' is a reversible soft-delete). Settle-up, a general
transaction edit (amount/split) or delete, and money movement deliberately have
NO tool here — the money line held on purpose (AGENT-DESIGN invariant 3),
recategorize being the one narrow, label-only exception. The pantry READ ('what do we need?', and finding an item's id) is the
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
        "name": "ledger_propose_rule",
        "description":
            "STEP 1 of 2 for an 'always do this' rule — e.g. 'always tag "
            "deposits like that as my paycheck' or 'those card payments are "
            "transfers, every month'. This step CHANGES NOTHING: it checks the "
            "rule and returns a preview — how many already-landed deposits it "
            "would match (would_match_now), a few sample rows, any existing "
            "rules that overlap, and a `warning` string when the rule is BROAD "
            "(a short phrase with nothing else narrowing it, which will also "
            "catch FUTURE deposits) — plus a single-use confirmation_token. "
            "REQUIRED next: tell the person the rule in one sentence and what "
            "the preview found; if a `warning` is present, read it to them "
            "before they decide (a broad transfer rule can hide a future "
            "paycheck). Only if they say yes in their OWN "
            "next reply do you call ledger_confirm_action with the token — "
            "never propose and confirm in the same turn, and never propose a "
            "rule they didn't ask for in their own words. If the preview looks "
            "wrong (it would catch things it shouldn't), say so and tighten "
            "the phrase instead. set_type='transfer' makes it a transfer rule "
            "(matches are kept out of income AND spending — right for "
            "credit-card payments and money moved between accounts). Fancier "
            "rules (amount limits, a specific account) happen in the app.",
        "input_schema": actions.param_schema("create_income_rule"),
        "execute": lambda caller, a: caller("POST", "/api/actions/propose", {
            "action_type": "create_rule",
            "set_type": a.get("set_type"),
            "match_desc": a.get("match_desc"),
            "set_transfer": a.get("set_type") == "transfer",
            "also_apply_to_existing": bool(a.get("also_apply_to_existing", True)),
        }),
    },
    {
        "name": "ledger_confirm_action",
        "description":
            "STEP 2 of 2: create the rule the person just approved. Pass the "
            "confirmation_token from ledger_propose_rule's reply — only after "
            "they said yes to the preview in their own reply, and only in a "
            "LATER turn than the propose. It executes exactly what was "
            "previewed (the server holds the frozen details; nothing you pass "
            "here can change them). Single-use and it expires — if it's "
            "refused, propose again rather than retrying. Afterwards, say "
            "plainly what the rule now does and how many old deposits were "
            "tagged; a rule can be switched off in the app.",
        "input_schema": actions.param_schema("confirm_action"),
        "execute": lambda caller, a: caller("POST", "/api/actions/confirm", {
            "confirmation_token": a.get("confirmation_token")}),
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
    {
        "name": "ledger_set_budget",
        "description":
            "Set or change a category's MONTHLY spending limit ('budget $400 a "
            "month for groceries', 'bump dining to $250'). A budget is just a "
            "target the app tracks spending against — it moves NO money, isn't "
            "a payment, and never changes the who-owes-whom balance. CONFIRM "
            "FIRST: read back the category and the dollar limit and set it only "
            "once they say yes — don't invent a number they didn't give. Check "
            "ledger_budget_status first so you use an existing category's exact "
            "name (setting the same category again just changes its limit; a "
            "new name starts a new budget). Amount is dollars, must be "
            "positive. Logged and reversible; removing a budget happens in the "
            "app.",
        "input_schema": actions.param_schema("set_budget"),
        "execute": lambda caller, a: caller("POST", "/api/budgets", {
            "category": a.get("category"), "amount": a.get("amount")}),
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
