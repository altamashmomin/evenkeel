"""ledger_mcp — MCP read tier for Ledger, the household finance app.

A *sibling process* to the Flask app (AGENT-DESIGN.md). It is an HTTP client
of the Flask API over localhost with a bearer token — never a second process
opening the SQLite file. It holds no state and does no math: every number a
tool returns comes from a Flask endpoint that runs the same derivation the
app's own dashboard runs, so the assistant can never disagree with the app or
hallucinate a total (AGENT-DESIGN invariants 1 and 2).

This module is the READ tier only. Every tool is read-only; the token it
carries is minted with `scopes='read'`, so the Flask API rejects any write
(HTTP 403) even if a tool tried. The write tools (classify, rules, the
two-phase confirm choreography) are a later increment.

Run it (over the tailnet, from the Pi):

    LEDGER_MCP_TOKEN=<a read token from Ledger settings> \
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
        "never gross_inflows (which includes refunds and transfers). This tier "
        "is read-only: recording settlements, editing transactions, and "
        "creating rules happen in the app, not here."
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


def _json(obj) -> str:
    """Serialize a tool result. Deterministic key order so a client (and the
    tests) see a stable shape."""
    return json.dumps(obj, indent=2, sort_keys=False)


_READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                        openWorldHint=False)


# ═════════════════════════════════ READ TIER ════════════════════════════════

@mcp.tool(name="ledger_household_snapshot", title="Household snapshot",
          annotations=_READ)
def ledger_household_snapshot(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    """One-call overview of the household for a month. START HERE for any
    open-ended question ("how are we doing?", "can we afford X?").

    Composes, with no math of its own: spend total + per-category (net of
    refunds), the who-owes-whom balance, income (gross_inflows, true_income,
    net_cash_flow, savings_rate, unclassified_count), goals, and this month's
    bills. Every money field is {cents, display}; quote `display` verbatim.

    'income' = true_income (paychecks only); gross_inflows also counts refunds
    and transfers and is NOT income. savings_rate is null when true income is
    0. If unclassified_count > 0, say the income figures are provisional and
    offer to tag (ledger_unclassified_inflows)."""
    return _json(api_get("/api/household_snapshot", {"month": month}))


@mcp.tool(name="ledger_balance", title="Who owes whom", annotations=_READ)
def ledger_balance() -> str:
    """The settle-up number: who owes whom, from shared-spending splits.
    Income NEVER affects this — a paycheck belongs to its earner and carries
    no share math. Recording a settlement happens in the app, not here."""
    return _json(api_get("/api/balance"))


@mcp.tool(name="ledger_spending_composition", title="Spending composition",
          annotations=_READ)
def ledger_spending_composition(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
    merchant_limit: Annotated[int, Field(
        default=10, ge=1, le=50,
        description="How many top merchants to return.")] = 10,
) -> str:
    """What made up a month's spending: each category's NET spend (refunds
    subtracted) with its `share` of the total, plus the top merchants by
    total paid (outflows only, settlements excluded). Money as {cents,
    display}. OUTFLOWS ONLY — money in never appears here; use the income
    tools for that. For totals over several months use ledger_category_trend
    or ledger_income_trend, not this."""
    return _json(api_get("/api/analytics/spending-composition",
                         {"month": month, "merchant_limit": merchant_limit}))


@mcp.tool(name="ledger_category_trend", title="Category trend",
          annotations=_READ)
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
    """Per-month NET spend for ONE category over a trailing window, with a
    trailing 3-month rolling average and month-over-month delta (null for the
    first month). Refund netting flows through, so a heavy-refund month can
    dip. Use for "are we spending more on X?". Money as {cents, display}."""
    return _json(api_get("/api/analytics/category-trend",
                         {"category": category, "months_back": months_back,
                          "anchor": anchor}))


@mcp.tool(name="ledger_income_summary", title="Income summary",
          annotations=_READ)
def ledger_income_summary(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    """Income aggregates for a month. The vocabulary matters — use it exactly:
      gross_inflows  = every deposit (paychecks + refunds + transfers + …)
      true_income    = paycheck rows ONLY. When the user says "income" they
                       mean this — never present gross_inflows as income.
      net_cash_flow  = true_income − spending
      savings_rate   = net_cash_flow / true_income (null when income is 0)
      unclassified_count = inflows still awaiting a type; if > 0 the numbers
                       above are provisional — say so and offer to tag.
    Money as dollars. Use ledger_income_trend for multiple months."""
    return _json(api_get("/api/income/summary", {"month": month}))


@mcp.tool(name="ledger_income_trend", title="Income vs. spend trend",
          annotations=_READ)
def ledger_income_trend(
    months_back: Annotated[int, Field(
        default=6, ge=1, le=24,
        description="Number of months ending at `anchor`.")] = 6,
    anchor: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="Last month of the window, 'YYYY-MM'. Omit for current.")] = None,
) -> str:
    """Per-month income vs. net spend over a trailing window — the data behind
    the analytics chart. Each month carries the same fields as
    ledger_income_summary (gross_inflows, true_income, net_cash_flow,
    savings_rate, unclassified_count). Empty months are zero-filled so the
    series is continuous. Use for "are we saving more over time?"."""
    return _json(api_get("/api/income/trend",
                         {"months_back": months_back, "anchor": anchor}))


@mcp.tool(name="ledger_savings_rate_trend", title="Savings-rate trend",
          annotations=_READ)
def ledger_savings_rate_trend(
    months_back: Annotated[int, Field(
        default=6, ge=1, le=24,
        description="Number of months ending at `anchor`.")] = 6,
    anchor: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="Last month of the window, 'YYYY-MM'. Omit for current.")] = None,
) -> str:
    """Per-month savings rate plus a trailing 3-month ROLLING rate that
    smooths single-month noise (cumulative net cash flow ÷ cumulative true
    income — income-weighted, not an average of ratios). Both are ratios
    (0.58 = 58%), not money; null when the relevant income is 0."""
    return _json(api_get("/api/analytics/savings-rate-trend",
                         {"months_back": months_back, "anchor": anchor}))


@mcp.tool(name="ledger_member_breakdown", title="Per-member breakdown",
          annotations=_READ)
def ledger_member_breakdown(
    month: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    """Per-member shared-expense breakdown for a month: each person's paid
    (fronted) vs owed (fair share) vs net; nets sum to zero. Shared OUTFLOWS
    only — income is excluded. Complements ledger_balance (the single
    who-owes-whom number) with the per-person composition. Money as {cents,
    display}."""
    return _json(api_get("/api/analytics/member-breakdown", {"month": month}))


@mcp.tool(name="ledger_bill_variance", title="Bill vs. actual",
          annotations=_READ)
def ledger_bill_variance(
    period: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM'. Omit for the current month.")] = None,
) -> str:
    """Defined bill amount vs what actually got paid, per bill, for a period.
    variance = actual − defined (positive = over). Unpaid bills report
    actual/variance = null. Money as {cents, display}."""
    return _json(api_get("/api/analytics/bill-variance", {"period": period}))


@mcp.tool(name="ledger_list_income_rules", title="List income rules",
          annotations=_READ)
def ledger_list_income_rules() -> str:
    """All income-classification rules in priority order (lower priority runs
    first; first match wins), each with its enabled flag and hit_count. A
    disabled rule keeps its history and drops out of matching. A rule with
    hit_count 0 after a month is probably dead — worth mentioning."""
    return _json(api_get("/api/income/rules"))


@mcp.tool(name="ledger_unclassified_inflows", title="Inflow tagging queue",
          annotations=_READ)
def ledger_unclassified_inflows(
    limit: Annotated[int, Field(
        default=50, ge=1, le=100,
        description="Max rows to return (most recent first).")] = 50,
) -> str:
    """The tagging queue: inflows still typed 'unclassified' (money in whose
    kind isn't known yet), most recent first. Present each with a suggested
    type AND your reason, let the user confirm or correct — then tagging
    happens in the app (this read tier can't write). While these exist, income
    totals are provisional. Returns the same shape as
    ledger_search_transactions."""
    return _json(api_get("/api/transactions/search",
                         {"direction": "in", "income_type": "unclassified",
                          "limit": limit}))


@mcp.tool(name="ledger_search_transactions", title="Search transactions",
          annotations=_READ)
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
    """Find specific transactions — the EVIDENCE tool ("show me the three
    biggest grocery runs", "did the deposit land?"). NOT for totals: those
    come from the summary/trend tools, computed once by the app's own code.
    All filters are optional and ANDed. Paginated: returns total_matches and
    has_more, so page rather than extrapolate. Money as {cents, display}."""
    return _json(api_get("/api/transactions/search", {
        "query": query, "date_from": date_from, "date_to": date_to,
        "direction": direction, "income_type": income_type,
        "category": category, "paid_by": paid_by,
        "limit": limit, "offset": offset,
    }))


@mcp.tool(name="ledger_list_goals_and_bills", title="Goals and bills",
          annotations=_READ)
def ledger_list_goals_and_bills(
    period: Annotated[Optional[str], Field(
        default=None, pattern=r"^\d{4}-\d{2}$",
        description="ISO month 'YYYY-MM' for bill paid/unpaid status. Omit "
                    "for the current month.")] = None,
) -> str:
    """Savings goals (target, saved-so-far, %) and recurring bills (name,
    amount, due day, category, and whether this period's payment has landed).
    Read-only — goal and bill management lives in the app. Money as dollars."""
    return _json({
        "goals": api_get("/api/goals"),
        "bills": api_get("/api/bills", {"period": period}),
    })


if __name__ == "__main__":
    if not os.environ.get("LEDGER_MCP_TOKEN"):
        raise SystemExit(
            "LEDGER_MCP_TOKEN is not set. Mint a 'read' token in Ledger's "
            "settings and export it before starting the MCP server.")
    mcp.run(transport="streamable-http")
