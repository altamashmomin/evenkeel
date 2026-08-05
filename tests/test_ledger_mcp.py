"""ledger_mcp read tier: the FastMCP sibling process (AGENT-DESIGN build-order
step 2). It holds no state and does no math — every tool is a thin HTTP client
of the Flask read API under a 'read' bearer token. These tests wire httpx's
WSGITransport straight at the Flask app in-process (no socket), mint a real
read token via the verb, and drive the tools through FastMCP's own dispatch
(call_tool), so validation and registration are exercised too.

The load-bearing assertions are the passthrough ones: a tool's JSON is
byte-equal to the Flask endpoint it wraps. That IS the 'does no math'
invariant — the MCP layer can only reshape, never recompute, so it can never
disagree with the app's dashboard."""
import asyncio
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"

# Imported once; the singleton FastMCP registry is fine — each test re-injects
# its own client + token in setUp.
sys.path.insert(0, str(REPO))
import ledger_mcp  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

READ_TOOLS = {
    "ledger_household_snapshot", "ledger_balance",
    "ledger_spending_composition", "ledger_category_trend",
    "ledger_income_summary", "ledger_income_trend",
    "ledger_savings_rate_trend", "ledger_member_breakdown",
    "ledger_bill_variance", "ledger_list_income_rules",
    "ledger_unclassified_inflows", "ledger_search_transactions",
    "ledger_list_goals_and_bills", "ledger_inventory",
}


class LedgerMcpReadTierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-mcp-test-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "72", "--months", "3", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "seed_income.py"), str(self.db_path)],
            check=True, capture_output=True, text=True)

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "mcp-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_mcp_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

        # Mint a real read token; drive Flask in-process via WSGITransport.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        import actions
        self.token = actions.create_api_token(
            conn, "ui:avery", {"label": "claude-code", "user_id": 1,
                               "scopes": "read"})["token"]
        conn.close()
        os.environ["LEDGER_MCP_TOKEN"] = self.token
        ledger_mcp.set_client(httpx.Client(
            transport=httpx.WSGITransport(app=self.app_module.app),
            base_url="http://ledger.test"))

    def tearDown(self):
        ledger_mcp.set_client(None)
        os.environ.pop("LEDGER_MCP_TOKEN", None)
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------------
    def call(self, name, **args):
        """Dispatch a tool through FastMCP and return its parsed JSON."""
        _content, structured = asyncio.run(ledger_mcp.mcp.call_tool(name, args))
        return json.loads(structured["result"])

    def direct(self, path, **params):
        """Hit the Flask endpoint directly with the same bearer token."""
        return self.app_module.app.test_client().get(
            path, query_string=params,
            headers={"Authorization": f"Bearer {self.token}"}).get_json()

    # -- registration ---------------------------------------------------------
    def test_all_read_tools_registered_and_read_only(self):
        by_name = {t.name: t for t in asyncio.run(ledger_mcp.mcp.list_tools())}
        self.assertTrue(READ_TOOLS <= set(by_name),
                        "a read tool went missing from ledger_mcp")
        for name in READ_TOOLS:  # write tools live alongside and are not read-only
            t = by_name[name]
            self.assertTrue(t.annotations and t.annotations.readOnlyHint,
                            f"{name} is not marked read-only")

    # -- passthrough (the 'does no math' proof) -------------------------------
    def test_household_snapshot_matches_direct_api(self):
        self.assertEqual(self.direct("/api/household_snapshot"),
                         self.call("ledger_household_snapshot"))

    def test_snapshot_respects_month_arg(self):
        body = self.call("ledger_household_snapshot", month="2026-06")
        self.assertEqual("2026-06", body["month"])
        self.assertEqual(self.direct("/api/household_snapshot", month="2026-06"),
                         body)

    def test_balance_matches_direct_api(self):
        self.assertEqual(self.direct("/api/balance"),
                         self.call("ledger_balance"))

    def test_income_summary_matches_direct_api(self):
        self.assertEqual(self.direct("/api/income/summary"),
                         self.call("ledger_income_summary"))

    def test_income_trend_matches_direct_api(self):
        body = self.call("ledger_income_trend", months_back=3)
        self.assertEqual(self.direct("/api/income/trend", months_back=3), body)
        self.assertEqual(3, len(body["series"]))

    def test_search_matches_direct_api_with_filters(self):
        body = self.call("ledger_search_transactions", direction="out",
                         limit=5)
        self.assertEqual(
            self.direct("/api/transactions/search", direction="out", limit=5),
            body)
        self.assertTrue(all(t["direction"] == "out"
                            for t in body["transactions"]))

    # -- convenience-wrapper semantics ----------------------------------------
    def test_unclassified_inflows_are_all_in_and_unclassified(self):
        body = self.call("ledger_unclassified_inflows")
        self.assertGreater(body["total_matches"], 0)  # seed_income makes some
        for t in body["transactions"]:
            self.assertEqual("in", t["direction"])
            self.assertEqual("unclassified", t["income_type"])

    def test_goals_and_bills_merges_both_endpoints(self):
        body = self.call("ledger_list_goals_and_bills")
        self.assertEqual({"goals", "bills"}, set(body))
        self.assertEqual(self.direct("/api/goals"), body["goals"])
        self.assertEqual(self.direct("/api/bills"), body["bills"])

    def test_inventory_matches_direct_api(self):
        body = self.call("ledger_inventory")
        self.assertEqual(self.direct("/api/inventory"), body)
        self.assertEqual(
            {"items", "shopping", "low_count", "restock_suggestions", "restock_forecast"},
            set(body))

    # -- money shape ----------------------------------------------------------
    def test_money_fields_are_cents_and_display(self):
        total = self.call("ledger_household_snapshot")["spend"]["total"]
        self.assertEqual({"cents", "display"}, set(total))
        self.assertIsInstance(total["cents"], int)
        self.assertTrue(total["display"].startswith("$")
                        or total["display"].startswith("-$"))

    # -- error mapping --------------------------------------------------------
    def test_bad_token_maps_to_actionable_error(self):
        os.environ["LEDGER_MCP_TOKEN"] = "not-a-real-token"
        with self.assertRaises(ToolError) as ctx:
            self.call("ledger_household_snapshot")
        msg = str(ctx.exception)
        self.assertIn("401", msg)
        self.assertIn("token", msg.lower())

    def test_api_400_is_surfaced(self):
        with self.assertRaises(ToolError) as ctx:
            # category-trend requires a category; empty string is a 400.
            self.call("ledger_category_trend", category=" ")
        self.assertIn("category", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
