"""agent_write_tools — the write-tool surface for Charlee's in-app Ask loop.

Kept SEPARATE from agent_read_tools (which is read-only by construction): the
in-app assistant may TAG inflows, and only that. Each tool bottoms out in the
same Flask write ROUTE the SPA uses (one write path, CORE-DESIGN invariant 2),
executed in-process under the caller's own session — so the write is attributed
to the person (`ui:<name>`), validated by the verb, logged, and reversible.

v1 exposes exactly one tool: classify_inflow. Rules (create_income_rule /
apply_rules, two-phase) are a later increment; settle-up, transaction edit or
delete, and money movement deliberately have NO tool here — ACL by omission
(AGENT-DESIGN invariant 3).
"""
from typing import Callable

# The real income types a human can assign — 'unclassified' is the *absence* of
# a tag, never a target, so it isn't offered.
REAL_INCOME_TYPES = ["paycheck", "reimbursement", "refund",
                     "transfer", "gift", "other"]


def _obj(props=None, required=None):
    return {"type": "object", "properties": props or {},
            "required": required or [], "additionalProperties": False}


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
        "input_schema": _obj({
            "transaction_id": {
                "type": "integer",
                "description": "The money-in row's id (from a search or the "
                               "unclassified-inflows queue)."},
            "income_type": {
                "type": "string", "enum": REAL_INCOME_TYPES,
                "description": "What kind of income it is, per the person's "
                               "own words."},
        }, required=["transaction_id", "income_type"]),
        "execute": lambda caller, a: caller(
            "PUT", f"/api/transactions/{a['transaction_id']}/classify",
            {"income_type": a.get("income_type")}),
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
