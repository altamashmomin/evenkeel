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


if __name__ == "__main__":
    if not os.environ.get("LEDGER_MCP_TOKEN"):
        raise SystemExit(
            "LEDGER_MCP_TOKEN is not set. Mint a 'read' token in Ledger's "
            "settings and export it before starting the MCP server.")
    mcp.run(transport="streamable-http")
