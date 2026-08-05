"""ledger_mcp — MCP read tier for Ledger, the household finance app.

A *sibling process* to the Flask app (AGENT-DESIGN.md). It is an HTTP client
of the Flask API over localhost with a bearer token — never a second process
opening the SQLite file. It holds no state and does no math: every number a
tool returns comes from a Flask endpoint that runs the same derivation the
app's own dashboard runs, so the assistant can never disagree with the app or
hallucinate a total (AGENT-DESIGN invariants 1 and 2).

Tiers (AGENT-DESIGN's blast-radius ceremony): 13 READ tools; two DIRECT
writes (classify one inflow, enable/disable one rule — logged, reversible,
single-row); and a TWO-PHASE write tier (propose a rule / propose a backlog
sweep → the human approves the computed preview → confirm the frozen action).
The read tools work under a `read` token; every write tool needs a
`read,write` token, so the Flask API rejects writes (HTTP 403) from a read
token even if a tool tried. Dangerous operations (settle-up, transaction
edit/delete, money movement) deliberately don't exist as tools.

Run it (over the tailnet, from the Pi):

    LEDGER_MCP_TOKEN=<a token from Ledger settings — 'read,write' for writes> \
    LEDGER_API_BASE=http://127.0.0.1:8080 \
    LEDGER_MCP_HOST=127.0.0.1 LEDGER_MCP_PORT=8765 \
        python ledger_mcp.py

Reachability is the tailnet: bind `LEDGER_MCP_HOST` to the Pi's Tailscale IP
(or 127.0.0.1 behind a tunnel) and connect from Claude Code/Desktop over
Tailscale. No Tailscale Funnel, no public exposure (AGENT-DESIGN decision #1).
"""
import json
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from typing import Annotated, Optional

from agent_read_tools import DESCRIPTIONS  # the shared, single-source tool docs

API_BASE = os.environ.get("LEDGER_API_BASE", "http://127.0.0.1:8080")

mcp = FastMCP(
    "ledger",
    host=os.environ.get("LEDGER_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("LEDGER_MCP_PORT", "8765")),
    instructions=(
        "Ledger is a two-person household finance app. Answer money questions "
        "from these tools, never from your own arithmetic — every total is "
        "computed server-side by the same code the app's dashboard uses. Start "
        "with ledger_household_snapshot for open-ended questions. Money fields "
        "arrive as {cents, display}; quote the `display` string verbatim and "
        "never convert units yourself. 'income' means true_income (paychecks), "
        "never gross_inflows (which includes refunds and transfers). "
        "ledger_inventory reads the household pantry (staples + shopping list) — "
        "groceries and supplies, not money; changing it happens in the app. "
        "You can tag inflows and manage income rules, but only after the user confirms "
        "in their own words — and creating a rule or sweeping the backlog is "
        "two-phase: propose, show the user the preview, get an explicit yes, "
        "then confirm. Recording settlements, editing or deleting transactions, "
        "and moving money are NOT possible here — those happen in the app."
    ),
)


# ── shared plumbing ─────────────────────────────────────────────────────────

class LedgerAPIError(RuntimeError):
    """Raised for any non-2xx from the Flask API or an unreachable server.
    FastMCP surfaces the message to the agent as a tool error, so the text is
    written to be actionable for whoever is driving the conversation."""


_client: Optional[httpx.Client] = None


def get_client() -> httpx.Client:
    """The lazily-built httpx client. Tests replace it with `set_client` to
    drive the Flask WSGI app in-process (no socket)."""
    global _client
    if _client is None:
        _client = httpx.Client(base_url=API_BASE, timeout=10.0)
    return _client


def set_client(client: Optional[httpx.Client]) -> None:
    """Test seam: inject an httpx client (e.g. one wired to a WSGITransport
    over the Flask app) or reset with None."""
    global _client
    _client = client


def api_get(path: str, params: Optional[dict] = None) -> dict:
    """GET `path` on the Flask API with the read bearer token, returning the
    decoded JSON. Maps transport and HTTP errors to actionable messages."""
    token = os.environ.get("LEDGER_MCP_TOKEN", "")
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = get_client().get(
            path, params=clean,
            headers={"Authorization": f"Bearer {token}"})
    except httpx.RequestError as e:
        raise LedgerAPIError(
            f"Cannot reach the Ledger API at {API_BASE} ({e}). Is the "
            "pifinance service running on the Pi?") from e
    if resp.status_code == 401:
        raise LedgerAPIError(
            "Ledger rejected the token (401): it is missing, revoked, or "
            "expired. Ask the user to issue a new 'read' token in Ledger's "
            "settings and set LEDGER_MCP_TOKEN.")
    if resp.status_code == 403:
        raise LedgerAPIError(
            "This token lacks 'read' scope (403). It cannot be used for the "
            "read tier.")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", resp.text)
        except (ValueError, AttributeError):
            detail = resp.text
        raise LedgerAPIError(f"Ledger API error {resp.status_code}: {detail}")
    return resp.json()


def api_write(method: str, path: str, body: Optional[dict] = None) -> dict:
    """Send a mutating request (PUT/POST) to the Flask API with the bearer
    token, returning the decoded JSON. Write tools need a `read,write` token —
    a `read` token is rejected 403. A 4xx from a verb (e.g. a rule conflict, or
    confirming an expired/consumed token) carries the API's own message, which
    is written for a human, so it is surfaced verbatim for the agent to relay."""
    token = os.environ.get("LEDGER_MCP_TOKEN", "")
    try:
        resp = get_client().request(
            method, path, json=body or {},
            headers={"Authorization": f"Bearer {token}"})
    except httpx.RequestError as e:
        raise LedgerAPIError(
            f"Cannot reach the Ledger API at {API_BASE} ({e}). Is the "
            "pifinance service running on the Pi?") from e
    if resp.status_code == 401:
        raise LedgerAPIError(
            "Ledger rejected the token (401): it is missing, revoked, or "
            "expired. Ask the user to issue a new 'read,write' token in "
            "Ledger's settings and set LEDGER_MCP_TOKEN.")
    if resp.status_code == 403:
        raise LedgerAPIError(
            "This token lacks 'write' scope (403). Write tools need a "
            "'read,write' token — ask the user to mint one in Ledger and "
            "update LEDGER_MCP_TOKEN.")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", resp.text)
        except (ValueError, AttributeError):
            detail = resp.text
        raise LedgerAPIError(f"Ledger API error {resp.status_code}: {detail}")
    return resp.json()


def _json(obj) -> str:
    """Serialize a tool result. Deterministic key order so a client (and the
    tests) see a stable shape."""
    return json.dumps(obj, indent=2, sort_keys=False)


_READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                        openWorldHint=False)


# ═════════════════════════════════ READ TIER ════════════════════════════════

@mcp.tool(name="ledger_household_snapshot", title="Household snapshot",
          description=DESCRIPTIONS["ledger_household_snapshot"], annotations=_READ)
def ledger_household_snapshot(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    return _json(api_get("/api/household_snapshot", {"month": month}))


@mcp.tool(name="ledger_balance", title="Who owes whom",
          description=DESCRIPTIONS["ledger_balance"], annotations=_READ)
def ledger_balance() -> str:
    return _json(api_get("/api/balance"))


@mcp.tool(name="ledger_spending_composition", title="Spending composition",
          description=DESCRIPTIONS["ledger_spending_composition"], annotations=_READ)
def ledger_spending_composition(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
    merchant_limit: Annotated[int, Field(
        default=10, ge=1, le=50,
        description="How many top merchants to return.")] = 10,
) -> str:
    return _json(api_get("/api/analytics/spending-composition",
                         {"month": month, "merchant_limit": merchant_limit}))


@mcp.tool(name="ledger_category_trend", title="Category trend",
          description=DESCRIPTIONS["ledger_category_trend"], annotations=_READ)
def ledger_category_trend(
    category: Annotated[str, Field(
        description="Category name, e.g. 'Groceries'. Case-sensitive; must "
                    "match the categories the app uses.")],
    months_back: Annotated[int, Field(
        default=6, ge=1, le=24,
        description="Number of months ending at `anchor`.")] = 6,
    anchor: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="Last month of the window, 'YYYY-MM'. Omit for current.")] = None,
) -> str:
    return _json(api_get("/api/analytics/category-trend",
                         {"category": category, "months_back": months_back,
                          "anchor": anchor}))


@mcp.tool(name="ledger_income_summary", title="Income summary",
          description=DESCRIPTIONS["ledger_income_summary"], annotations=_READ)
def ledger_income_summary(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    return _json(api_get("/api/income/summary", {"month": month}))


@mcp.tool(name="ledger_income_trend", title="Income vs. spend trend",
          description=DESCRIPTIONS["ledger_income_trend"], annotations=_READ)
def ledger_income_trend(
    months_back: Annotated[int, Field(
        default=6, ge=1, le=24,
        description="Number of months ending at `anchor`.")] = 6,
    anchor: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="Last month of the window, 'YYYY-MM'. Omit for current.")] = None,
) -> str:
    return _json(api_get("/api/income/trend",
                         {"months_back": months_back, "anchor": anchor}))


@mcp.tool(name="ledger_savings_rate_trend", title="Savings-rate trend",
          description=DESCRIPTIONS["ledger_savings_rate_trend"], annotations=_READ)
def ledger_savings_rate_trend(
    months_back: Annotated[int, Field(
        default=6, ge=1, le=24,
        description="Number of months ending at `anchor`.")] = 6,
    anchor: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="Last month of the window, 'YYYY-MM'. Omit for current.")] = None,
) -> str:
    return _json(api_get("/api/analytics/savings-rate-trend",
                         {"months_back": months_back, "anchor": anchor}))


@mcp.tool(name="ledger_member_breakdown", title="Per-member breakdown",
          description=DESCRIPTIONS["ledger_member_breakdown"], annotations=_READ)
def ledger_member_breakdown(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    return _json(api_get("/api/analytics/member-breakdown", {"month": month}))


@mcp.tool(name="ledger_bill_variance", title="Bill vs. actual",
          description=DESCRIPTIONS["ledger_bill_variance"], annotations=_READ)
def ledger_bill_variance(
    period: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    return _json(api_get("/api/analytics/bill-variance", {"period": period}))


@mcp.tool(name="ledger_recurring_charges", title="Recurring charges",
          description=DESCRIPTIONS["ledger_recurring_charges"], annotations=_READ)
def ledger_recurring_charges() -> str:
    return _json(api_get("/api/analytics/recurring"))


@mcp.tool(name="ledger_cash_flow_forecast", title="Cash-flow forecast",
          description=DESCRIPTIONS["ledger_cash_flow_forecast"], annotations=_READ)
def ledger_cash_flow_forecast(
    period: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    return _json(api_get("/api/analytics/cash-flow-forecast", {"period": period}))


@mcp.tool(name="ledger_goal_pace", title="Goal pace",
          description=DESCRIPTIONS["ledger_goal_pace"], annotations=_READ)
def ledger_goal_pace(
    as_of: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="ISO date to project from. Omit for today.")] = None,
) -> str:
    return _json(api_get("/api/analytics/goal-pace", {"as_of": as_of}))


@mcp.tool(name="ledger_anomaly_flags", title="Spending anomalies",
          description=DESCRIPTIONS["ledger_anomaly_flags"], annotations=_READ)
def ledger_anomaly_flags(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
    threshold: Annotated[Optional[int], Field(
        default=None, ge=1,
        description="Percent over the 3-month average to flag (default 50).")] = None,
) -> str:
    return _json(api_get("/api/analytics/anomalies",
                         {"month": month, "threshold": threshold}))


@mcp.tool(name="ledger_list_income_rules", title="List income rules",
          description=DESCRIPTIONS["ledger_list_income_rules"], annotations=_READ)
def ledger_list_income_rules() -> str:
    return _json(api_get("/api/income/rules"))


@mcp.tool(name="ledger_unclassified_inflows", title="Inflow tagging queue",
          description=DESCRIPTIONS["ledger_unclassified_inflows"], annotations=_READ)
def ledger_unclassified_inflows(
    limit: Annotated[int, Field(
        default=50, ge=1, le=100,
        description="Max rows to return (most recent first).")] = 50,
) -> str:
    return _json(api_get("/api/transactions/search",
                         {"direction": "in", "income_type": "unclassified",
                          "limit": limit}))


@mcp.tool(name="ledger_search_transactions", title="Search transactions",
          description=DESCRIPTIONS["ledger_search_transactions"], annotations=_READ)
def ledger_search_transactions(
    query: Annotated[Optional[str], Field(
        default=None, max_length=80,
        description="Case-insensitive substring on the description, e.g. "
                    "'amazon'.")] = None,
    date_from: Annotated[Optional[str], Field(
        default=None, description="Inclusive ISO date 'YYYY-MM-DD'.")] = None,
    date_to: Annotated[Optional[str], Field(
        default=None, description="Inclusive ISO date 'YYYY-MM-DD'.")] = None,
    direction: Annotated[Optional[str], Field(
        default=None, description="'in' (money in) or 'out' (spending).")] = None,
    income_type: Annotated[Optional[str], Field(
        default=None,
        description="One of paycheck, reimbursement, refund, transfer, gift, "
                    "other, unclassified. Only meaningful for direction='in'.")] = None,
    category: Annotated[Optional[str], Field(
        default=None, description="Exact category name.")] = None,
    paid_by: Annotated[Optional[str], Field(
        default=None,
        description="Username. For inflows this means the money's OWNER.")] = None,
    limit: Annotated[int, Field(default=20, ge=1, le=100)] = 20,
    offset: Annotated[int, Field(default=0, ge=0)] = 0,
) -> str:
    return _json(api_get("/api/transactions/search", {
        "query": query, "date_from": date_from, "date_to": date_to,
        "direction": direction, "income_type": income_type,
        "category": category, "paid_by": paid_by,
        "limit": limit, "offset": offset,
    }))


@mcp.tool(name="ledger_list_goals_and_bills", title="Goals and bills",
          description=DESCRIPTIONS["ledger_list_goals_and_bills"], annotations=_READ)
def ledger_list_goals_and_bills(
    period: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM' for bill paid/unpaid status. Omit "
                    "for the current month.")] = None,
) -> str:
    return _json({
        "goals": api_get("/api/goals"),
        "bills": api_get("/api/bills", {"period": period}),
    })


@mcp.tool(name="ledger_inventory", title="Household pantry",
          description=DESCRIPTIONS["ledger_inventory"], annotations=_READ)
def ledger_inventory() -> str:
    return _json(api_get("/api/inventory"))


# ═══════════════════════════ DIRECT WRITE TIER ══════════════════════════════
# Logged, reversible, single-row/flag — no preview (AGENT-DESIGN's routine
# teaching actions; a two-call dance for every tag would make the agent worse
# than the app at the one job it's best at). Every write needs a read,write
# token.

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                         idempotentHint=True, openWorldHint=False)
_PROPOSE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                           idempotentHint=False, openWorldHint=False)


@mcp.tool(
    name="ledger_classify_inflow", title="Classify an inflow",
    description=(
        "Set the income_type on ONE inflow, after the user has confirmed the "
        "type in their own words. DIRECT write — no preview: it's the routine "
        "tagging action, touches one row, is reversible (call again with a "
        "different type), and is logged. Only valid on money-IN rows (the API "
        "rejects outflows). Effect on the numbers you must respect when "
        "explaining: paycheck → counts toward true_income; refund → nets "
        "against its category's spending; transfer → excluded from every "
        "aggregate; reimbursement → excluded from income, NOT netted against "
        "spending; gift/other → excluded from income. Returns the updated row. "
        "If this description will clearly recur, offer a rule next "
        "(ledger_propose_income_rule)."),
    annotations=_WRITE)
def ledger_classify_inflow(
    transaction_id: Annotated[int, Field(
        description="The inflow's transaction id (from a search or the "
                    "unclassified-inflows queue).")],
    income_type: Annotated[str, Field(
        description="One of: paycheck, reimbursement, refund, transfer, gift, "
                    "other.")],
) -> str:
    return _json(api_write(
        "PUT", f"/api/transactions/{transaction_id}/classify",
        {"income_type": income_type}))


@mcp.tool(
    name="ledger_set_rule_enabled", title="Enable/disable a rule",
    description=(
        "Enable or disable ONE income-classification rule after the user asks. "
        "DIRECT write, logged. Disabling never reclassifies existing rows — it "
        "only stops future matches; there is deliberately no delete (a disabled "
        "rule keeps its history). Find the rule id via ledger_list_income_rules."),
    annotations=_WRITE)
def ledger_set_rule_enabled(
    rule_id: Annotated[int, Field(
        description="The rule id (from ledger_list_income_rules).")],
    enabled: Annotated[bool, Field(
        description="True to enable, false to disable.")],
) -> str:
    return _json(api_write(
        "PUT", f"/api/income/rules/{rule_id}", {"enabled": enabled}))


# ═══════════════════════════ TWO-PHASE WRITE TIER ═══════════════════════════
# A rule touches all FUTURE data and can bulk-apply backwards — that's the
# blast-radius line that earns ceremony. Propose parks a frozen action + a
# computed preview; the human approves the preview; confirm executes exactly
# that frozen action. Never propose and confirm in the same turn.

@mcp.tool(
    name="ledger_propose_income_rule", title="Propose income rule (preview)",
    description=(
        "PHASE 1 of 2 — does NOT create the rule. Validates it and dry-runs the "
        "matcher, parks the proposal server-side, and returns "
        "{confirmation_token, expires_at, preview}. The preview has "
        "would_match_now (current unclassified inflows this rule would "
        "classify), sample_rows, conflicts (existing enabled rules that also "
        "match those rows), and future_effect. REQUIRED next step: show the "
        "user the count, a sample, and any conflicts, and ask. Only after the "
        "user approves IN THEIR OWN REPLY may you call ledger_confirm_action "
        "with the token. Never propose and confirm in the same turn. If a match "
        "looks wrong (a transfer caught by a paycheck rule), tighten the "
        "matcher and propose again. also_apply_to_existing (default true) — on "
        "confirm, reclassify exactly the previewed rows (this new rule only); "
        "set false to create the rule without touching existing rows."),
    annotations=_PROPOSE)
def ledger_propose_income_rule(
    set_type: Annotated[str, Field(
        description="Type to assign on a match: paycheck, reimbursement, "
                    "refund, transfer, gift, other.")],
    match_desc: Annotated[Optional[str], Field(
        default=None, max_length=200,
        description="Case-insensitive substring of the description, e.g. 'ADP "
                    "PAYROLL'. Prefer the stable prefix; trailing digits vary "
                    "per deposit.")] = None,
    match_account: Annotated[Optional[str], Field(
        default=None,
        description="SimpleFIN account id, or omit for any account.")] = None,
    min_cents: Annotated[Optional[int], Field(
        default=None, ge=0, description="Minimum amount in cents.")] = None,
    max_cents: Annotated[Optional[int], Field(
        default=None, ge=0,
        description="Maximum amount in cents. Amount bounds catch collisions "
                    "(only deposits over $X from this payer are the paycheck).")] = None,
    set_paid_by: Annotated[Optional[int], Field(
        default=None,
        description="Member id to set as the money's OWNER on matched inflows; "
                    "omit to keep each row's existing owner. Member ids appear "
                    "in ledger_member_breakdown / ledger_household_snapshot.")] = None,
    priority: Annotated[Optional[int], Field(
        default=None,
        description="Lower runs first among rules (default 0).")] = None,
    also_apply_to_existing: Annotated[bool, Field(
        default=True,
        description="Whether confirming also reclassifies the current "
                    "unclassified rows this rule matches.")] = True,
) -> str:
    body = {
        "action_type": "create_rule", "set_type": set_type,
        "match_desc": match_desc, "match_account": match_account,
        "min_cents": min_cents, "max_cents": max_cents,
        "set_paid_by": set_paid_by, "priority": priority,
        "also_apply_to_existing": also_apply_to_existing,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return _json(api_write("POST", "/api/actions/propose", body))


@mcp.tool(
    name="ledger_apply_rules", title="Apply rules to the backlog (preview)",
    description=(
        "PHASE 1 of 2 — dry-runs ALL enabled rules over the current "
        "unclassified inflows and parks the proposal. Returns "
        "{confirmation_token, expires_at, preview:{rows_affected, by_rule:"
        "[{rule_id, count}]}}. Same contract as ledger_propose_income_rule: "
        "show the preview, get the user's yes in their own reply, then "
        "ledger_confirm_action. Use after adding or enabling rules to sweep the "
        "backlog."),
    annotations=_PROPOSE)
def ledger_apply_rules() -> str:
    return _json(api_write(
        "POST", "/api/actions/propose", {"action_type": "apply_rules"}))


@mcp.tool(
    name="ledger_confirm_action", title="Execute an approved action",
    description=(
        "PHASE 2 of 2 — executes exactly the parked proposal the token points "
        "to (the frozen payload, never your current arguments). Single-use; "
        "expires ~10 minutes after propose; a reused or expired token is "
        "refused — re-propose, never guess a token. ONLY call after the user "
        "has seen the preview and said yes in their own message. Returns what "
        "was executed."),
    annotations=_WRITE)
def ledger_confirm_action(
    confirmation_token: Annotated[str, Field(
        min_length=8, max_length=64,
        description="The token a propose tool returned, after the user "
                    "approved the preview.")],
) -> str:
    return _json(api_write(
        "POST", "/api/actions/confirm",
        {"confirmation_token": confirmation_token}))


if __name__ == "__main__":
    if not os.environ.get("LEDGER_MCP_TOKEN"):
        raise SystemExit(
            "LEDGER_MCP_TOKEN is not set. Mint a token in Ledger's settings "
            "('read', or 'read,write' to enable the write tools) and export it "
            "before starting the MCP server.")
    mcp.run(transport="streamable-http")
