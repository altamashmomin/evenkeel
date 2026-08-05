"""agent_read_tools — the ONE read-tool surface both assistant doors consume.

Charlee's in-app Ask loop and Alta's `ledger_mcp` server answer the same
money questions from the same 13 reads; their *descriptions* are the product
(they're the only instructions the model reliably follows), so they must not
drift between the two doors. This module is that single source: each tool's
name, description, and input schema live here once.

What differs per door is only the *transport* — how a tool call turns into a
read. Both are modeled as a `getter(path, query) -> dict` that runs a Flask
read endpoint and returns its parsed JSON:
  - the in-app Ask loop passes an in-process getter (the app under the user's
    session) — no HTTP, no token;
  - `ledger_mcp` passes an httpx getter over the tailnet with a bearer token.
Because every tool bottoms out in a real read endpoint, it can only reshape,
never recompute — the agent never does math (AGENT-DESIGN invariant 1).

Read-only by construction: there is no write tool here.
"""
from typing import Callable

# ── shared schema fragments ─────────────────────────────────────────────────
_MONTH = {"type": "string", "pattern": r"^\d{4}-\d{2}$",
          "description": "ISO month 'YYYY-MM'. Omit for the current month."}
_ANCHOR = {"type": "string", "pattern": r"^\d{4}-\d{2}$",
           "description": "Last month of the window 'YYYY-MM'. Omit for current."}
_MONTHS_BACK = {"type": "integer", "minimum": 1, "maximum": 24,
                "description": "Number of months ending at the anchor."}


def _obj(props=None, required=None):
    return {"type": "object", "properties": props or {},
            "required": required or [], "additionalProperties": False}


# ── the registry ────────────────────────────────────────────────────────────
# Each entry: name, description (the product), input_schema (JSON Schema), and
# fetch(getter, args) -> dict. `fetch` is the only per-tool code; it names the
# read endpoint(s) and maps the model's args to query params.
READ_TOOLS = [
    {
        "name": "ledger_household_snapshot",
        "description":
            "One-call overview of the household for a month. START HERE for "
            "open-ended questions ('how are we doing?', 'can we afford X?'). "
            "Returns spend total + by-category (net of refunds), the "
            "who-owes-whom balance, income (gross_inflows, true_income, "
            "net_cash_flow, savings_rate, unclassified_count), goals, and this "
            "month's bills. Money fields are {cents, display}; quote `display`. "
            "'income' = true_income (paychecks); gross_inflows also counts "
            "refunds and transfers and is NOT income. savings_rate is null when "
            "true income is 0. If unclassified_count > 0, say the income figures "
            "are provisional and offer to tag (in the app).",
        "input_schema": _obj({"month": _MONTH}),
        "fetch": lambda g, a: g("/api/household_snapshot", {"month": a.get("month")}),
    },
    {
        "name": "ledger_balance",
        "description":
            "The settle-up number: who owes whom, from shared-spending splits. "
            "Income NEVER affects this. Recording a settlement happens in the "
            "app, not here.",
        "input_schema": _obj(),
        "fetch": lambda g, a: g("/api/balance", {}),
    },
    {
        "name": "ledger_spending_composition",
        "description":
            "What made up a month's spending: each category's NET spend "
            "(refunds subtracted) with its share of the total, plus the top "
            "merchants by total paid (outflows only, settlements excluded). "
            "OUTFLOWS ONLY — money in never appears here. For multi-month "
            "totals use ledger_category_trend or ledger_income_trend.",
        "input_schema": _obj({
            "month": _MONTH,
            "merchant_limit": {"type": "integer", "minimum": 1, "maximum": 50,
                               "description": "How many top merchants (default 10)."},
        }),
        "fetch": lambda g, a: g("/api/analytics/spending-composition",
                                {"month": a.get("month"),
                                 "merchant_limit": a.get("merchant_limit")}),
    },
    {
        "name": "ledger_category_trend",
        "description":
            "Per-month NET spend for ONE category over a trailing window, with "
            "a trailing 3-month rolling average and month-over-month delta "
            "(null for the first month). Use for 'are we spending more on X?'. "
            "Dollar amounts.",
        "input_schema": _obj({
            "category": {"type": "string",
                         "description": "Category name, e.g. 'Groceries'. Case-sensitive."},
            "months_back": _MONTHS_BACK, "anchor": _ANCHOR,
        }, required=["category"]),
        "fetch": lambda g, a: g("/api/analytics/category-trend",
                                {"category": a.get("category"),
                                 "months_back": a.get("months_back"),
                                 "anchor": a.get("anchor")}),
    },
    {
        "name": "ledger_income_summary",
        "description":
            "Income aggregates for a month. Vocabulary matters: gross_inflows = "
            "every deposit; true_income = paycheck rows ONLY (this is what "
            "'income' means — never present gross_inflows as income); "
            "net_cash_flow = true_income − spending; savings_rate = "
            "net_cash_flow / true_income (null when income is 0); "
            "unclassified_count = inflows awaiting a type (if > 0, the numbers "
            "are provisional — say so). Dollar amounts.",
        "input_schema": _obj({"month": _MONTH}),
        "fetch": lambda g, a: g("/api/income/summary", {"month": a.get("month")}),
    },
    {
        "name": "ledger_income_trend",
        "description":
            "Per-month income vs. net spend over a trailing window — each month "
            "carries the same fields as ledger_income_summary. Empty months are "
            "zero-filled. Use for 'are we saving more over time?'. Dollars.",
        "input_schema": _obj({"months_back": _MONTHS_BACK, "anchor": _ANCHOR}),
        "fetch": lambda g, a: g("/api/income/trend",
                                {"months_back": a.get("months_back"),
                                 "anchor": a.get("anchor")}),
    },
    {
        "name": "ledger_savings_rate_trend",
        "description":
            "Per-month savings rate plus a trailing 3-month ROLLING rate that "
            "smooths single-month noise (income-weighted). Both are ratios "
            "(0.58 = 58%), not money; null when the relevant income is 0.",
        "input_schema": _obj({"months_back": _MONTHS_BACK, "anchor": _ANCHOR}),
        "fetch": lambda g, a: g("/api/analytics/savings-rate-trend",
                                {"months_back": a.get("months_back"),
                                 "anchor": a.get("anchor")}),
    },
    {
        "name": "ledger_member_breakdown",
        "description":
            "Per-member shared-expense breakdown for a month: each person's "
            "paid (fronted) vs owed (fair share) vs net; nets sum to zero. "
            "Shared OUTFLOWS only. Complements ledger_balance with the "
            "per-person composition. Money as {cents, display}.",
        "input_schema": _obj({"month": _MONTH}),
        "fetch": lambda g, a: g("/api/analytics/member-breakdown",
                                {"month": a.get("month")}),
    },
    {
        "name": "ledger_bill_variance",
        "description":
            "Defined bill amount vs what actually got paid, per bill, for a "
            "period. variance = actual − defined (positive = over). Unpaid bills "
            "report actual/variance = null. Money as {cents, display}.",
        "input_schema": _obj({"period": _MONTH}),
        "fetch": lambda g, a: g("/api/analytics/bill-variance",
                                {"period": a.get("period")}),
    },
    {
        "name": "ledger_recurring_charges",
        "description":
            "Likely recurring charges / subscriptions: outflows that repeat at a "
            "stable amount on a regular cadence (weekly through yearly, or 'every "
            "~N days'). A SUGGESTION, not an authority — a coincidence is not a "
            "subscription — so it is conservative: same normalized merchant + "
            "IDENTICAL amount + at least 3 charges + gaps regular to ±40%. Each "
            "carries the detected cadence, interval_days, occurrences, first/last "
            "charge, and the next expected date. Money as {cents, display}. "
            "Expect it to be SPARSE until there are a few months of history — a "
            "monthly charge needs 3 months before it shows.",
        "input_schema": _obj(),
        "fetch": lambda g, a: g("/api/analytics/recurring", {}),
    },
    {
        "name": "ledger_cash_flow_forecast",
        "description":
            "Projected month-end NET CASH FLOW — a conservative FLOOR: net-so-far "
            "(true income − spend this month) minus the bills still unpaid this "
            "month (each listed with its due day). It is net FLOW, not an account "
            "balance, and does NOT assume a paycheck that hasn't landed yet — so "
            "real month-end is usually BETTER than projected_net. Goals are "
            "excluded (contributions aren't scheduled). Answers 'how does this "
            "month end?' / 'can we cover the bills still due?'. Money {cents, display}.",
        "input_schema": _obj({"period": _MONTH}),
        "fetch": lambda g, a: g("/api/analytics/cash-flow-forecast",
                                {"period": a.get("period")}),
    },
    {
        "name": "ledger_goal_pace",
        "description":
            "Per-goal completion projection at the LIFETIME-AVERAGE contribution "
            "rate (net saved ÷ time since the first contribution): each goal's "
            "saved vs target, a monthly rate, a projected finish date, and status "
            "vs its target_date — complete / on_track / behind / projected (no "
            "target) / no_pace (nothing to extrapolate yet). Answers 'when will "
            "we hit the vacation goal?' / 'are we on pace?'. Money {cents, display}; "
            "rate and projected_date are null when there's no net pace.",
        "input_schema": _obj({
            "as_of": {"type": "string",
                      "description": "ISO date 'YYYY-MM-DD' to project from. Omit for today."},
        }),
        "fetch": lambda g, a: g("/api/analytics/goal-pace", {"as_of": a.get("as_of")}),
    },
    {
        "name": "ledger_anomaly_flags",
        "description":
            "Categories spending unusually high in a month vs their trailing "
            "3-month average — a passive heads-up ('groceries are 60% over "
            "their recent norm'), not an authority. Each flag has the month's "
            "spend, the baseline, the delta, and pct_over. Refund-netted (same "
            "spend the dashboard shows). Noise-guarded (needs a real baseline + "
            "a meaningful dollar jump). `threshold` is the percent-over cutoff "
            "(default 50). Money {cents, display}. Empty when nothing's unusual.",
        "input_schema": _obj({
            "month": _MONTH,
            "threshold": {"type": "integer", "minimum": 1,
                          "description": "Percent over the 3-mo average to flag (default 50)."},
        }),
        "fetch": lambda g, a: g("/api/analytics/anomalies",
                                {"month": a.get("month"), "threshold": a.get("threshold")}),
    },
    {
        "name": "ledger_list_income_rules",
        "description":
            "All income-classification rules in priority order (first match "
            "wins), each with its enabled flag and hit_count. A disabled rule "
            "keeps its history; a rule with hit_count 0 after a month is "
            "probably dead.",
        "input_schema": _obj(),
        "fetch": lambda g, a: g("/api/income/rules", {}),
    },
    {
        "name": "ledger_unclassified_inflows",
        "description":
            "The tagging queue: inflows still typed 'unclassified' (money in "
            "whose kind isn't known yet), most recent first. While these exist, "
            "income totals are provisional. Present each with a suggested type "
            "and your reason; tagging itself happens in the app.",
        "input_schema": _obj({
            "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                      "description": "Max rows (default 50)."},
        }),
        "fetch": lambda g, a: g("/api/transactions/search",
                                {"direction": "in", "income_type": "unclassified",
                                 "limit": a.get("limit", 50)}),
    },
    {
        "name": "ledger_search_transactions",
        "description":
            "Find specific transactions — the EVIDENCE tool ('show me the three "
            "biggest grocery runs', 'did the deposit land?'). NOT for totals "
            "(use the summary/trend tools). All filters optional and ANDed. "
            "Paginated: returns total_matches and has_more, so page rather than "
            "extrapolate. Money as {cents, display}.",
        "input_schema": _obj({
            "query": {"type": "string", "maxLength": 80,
                      "description": "Case-insensitive substring on the description."},
            "date_from": {"type": "string", "description": "Inclusive ISO date."},
            "date_to": {"type": "string", "description": "Inclusive ISO date."},
            "direction": {"type": "string", "enum": ["in", "out"]},
            "income_type": {"type": "string",
                            "enum": ["paycheck", "reimbursement", "refund",
                                     "transfer", "gift", "other", "unclassified"]},
            "category": {"type": "string"},
            "paid_by": {"type": "string",
                        "description": "Username. For inflows, the money's owner."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0},
        }),
        "fetch": lambda g, a: g("/api/transactions/search", {
            "query": a.get("query"), "date_from": a.get("date_from"),
            "date_to": a.get("date_to"), "direction": a.get("direction"),
            "income_type": a.get("income_type"), "category": a.get("category"),
            "paid_by": a.get("paid_by"), "limit": a.get("limit"),
            "offset": a.get("offset")}),
    },
    {
        "name": "ledger_list_goals_and_bills",
        "description":
            "Savings goals (target, saved-so-far, %) and recurring bills (name, "
            "amount, due day, category, and whether this period's payment has "
            "landed). Read-only — goal and bill management lives in the app.",
        "input_schema": _obj({"period": _MONTH}),
        "fetch": lambda g, a: {"goals": g("/api/goals", {}),
                               "bills": g("/api/bills", {"period": a.get("period")})},
    },
    {
        "name": "ledger_inventory",
        "description":
            "The household pantry: the tracked staples (each stocked / low / "
            "out) plus the derived shopping list — everything that needs buying "
            "right now: staples that are low or out AND one-off needs — a count "
            "of staples running low, and restock_suggestions (low/out staples "
            "with a matching purchase since they ran low — hints someone may "
            "have restocked, to confirm, never assumed). Use this for 'what do "
            "we need?' / 'are we out of X?', and to find an item's `id` before "
            "changing its status. This is groceries and supplies, not money — "
            "nothing here touches the balance, spending, or income.",
        "input_schema": _obj(),
        "fetch": lambda g, a: g("/api/inventory", {}),
    },
]

_BY_NAME = {t["name"]: t for t in READ_TOOLS}

# The canonical descriptions, keyed by tool name. `ledger_mcp` imports this so
# the two doors share one copy — the descriptions never drift.
DESCRIPTIONS = {t["name"]: t["description"] for t in READ_TOOLS}


def anthropic_tools(cache: bool = True) -> list:
    """The tool list in Anthropic Messages API format. When `cache` is set,
    the last tool carries a cache_control breakpoint so the whole (static)
    tool block is prompt-cached — cheap repeat turns on a per-query bill."""
    tools = [{"name": t["name"], "description": t["description"],
              "input_schema": t["input_schema"]} for t in READ_TOOLS]
    if cache and tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def call_read_tool(getter: Callable[[str, dict], dict], name: str, args: dict) -> dict:
    """Execute one read tool via `getter(path, query)`. Raises KeyError for an
    unknown tool name (the model hallucinated one). The getter is responsible
    for auth and for raising on a non-2xx read."""
    spec = _BY_NAME.get(name)
    if spec is None:
        raise KeyError(f"unknown tool: {name}")
    return spec["fetch"](getter, args or {})
